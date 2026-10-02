import hashlib
import json
from pathlib import Path

import pytest
from datetime import datetime, timezone
from trading_desk.resource_budget import BudgetLedger, BudgetVector
from test_resource_budget import _full_limits, _task, _measured

from trading_desk.pipeline_checkpoint_transport import CheckpointObjectTransport, MAX_OBJECT_BYTES
from trading_desk.pipeline_runner import PipelineRunnerError
from test_pipeline_bundle_resume import Guard, Adapter, payload, run


class Response:
    def __init__(self, content, status=200, headers=None):
        self.content, self.status_code, self.headers = content, status, headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class Session:
    def __init__(self):
        self.calls, self.objects = [], {}
        self.lose_response = False
        self.status = None
        self.corrupt = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        p = kwargs["params"]
        key = f'market-data/pipeline/checkpoints/v1/{hashlib.sha256(p["runKey"].encode()).hexdigest()}/{p["producerBuildSha"]}/{p["stage"]}/{p["inputSha256"]}/{p["sha256"]}.json'
        if self.status is not None:
            return Response(b"{}", self.status)
        if method == "POST":
            self.objects.setdefault(key, kwargs["data"])
            if self.lose_response:
                raise TimeoutError("fixture simulated lost response, not production")
            return Response(json.dumps({"decision": "verified", "key": key, "sha256": p["sha256"], "bytes": len(kwargs["data"])}).encode())
        data = self.objects.get(key)
        if data is None:
            return Response(b"{}", 404)
        return Response(data + (b"x" if self.corrupt else b""), headers={"X-Flow-Checkpoint-Sha256": p["sha256"], "X-Flow-Checkpoint-Key": key})


def transport(session, guard=None, **kwargs):
    if "budget_ledger" not in kwargs:
        ledger = BudgetLedger(limits=_full_limits(), measured=_measured(),
            clock=lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
        # Explicit local fixture limits, not production account telemetry.
        from dataclasses import replace
        ledger.limits = replace(ledger.limits, r2_bytes=MAX_OBJECT_BYTES * 20)
        total = _task(100).numeric(); total["r2Bytes"] = MAX_OBJECT_BYTES * 12
        assert ledger.reserve("fixture-task", BudgetVector.estimated(total))["decision"] in {"allow", "warn"}
        cost = _task(4).numeric(); cost["r2Bytes"] = MAX_OBJECT_BYTES
        kwargs.update(budget_ledger=ledger, budget_task_id="fixture-task",
                      request_costs={method: BudgetVector.estimated(cost) for method in ("POST", "GET")})
    return CheckpointObjectTransport(base_url="https://flow.xiaoker.com", ingest_key="fixture-admin",
        access_client_id="fixture-id", access_client_secret="fixture-secret", lease_guard=guard or Guard(), session=session, **kwargs)


def test_transport_integrates_two_job_directories_without_source_rerun(tmp_path):
    from trading_desk.pipeline_runner import run_pipeline_request
    from datetime import datetime, timezone
    from test_pipeline_bundle_resume import ROOT, LOCK

    session = Session()
    first_guard = Guard()
    client = transport(session, first_guard)
    first = run_pipeline_request(payload(), state_dir=tmp_path / "one/state", output_path=tmp_path / "one/result.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        stage_adapters={"core_collection": Adapter()}, lease_guard=first_guard, export_bundles=True, checkpoint_transport=client)
    descriptor = first["checkpointBundles"]["exports"]["core_collection"]
    assert descriptor["remote"]["sha256"] == descriptor["sha256"]
    assert first["sideEffects"]["remoteWrites"] is True
    second_guard = Guard()
    second_client = transport(session, second_guard)
    adapter = Adapter()
    second = run_pipeline_request(payload(), state_dir=tmp_path / "two/state", output_path=tmp_path / "two/result.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        stage_adapters={"core_collection": adapter}, lease_guard=second_guard, checkpoint_transport=second_client,
        resume_references={"core_collection": descriptor["sha256"]})
    assert second["status"] == "COMPLETED" and adapter.runs == 0
    assert second["sideEffects"]["remoteWrites"] is False
    assert second["checkpointTransportEvidence"]["accountRemaining"] is None
    assert [call[0] for call in session.calls] == ["POST", "GET"]


def source(tmp_path):
    from trading_desk.pipeline_runner import build_execution_context
    from datetime import datetime, timezone
    request = payload()
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    report = run(tmp_path)
    ref = report["checkpointBundles"]["exports"]["core_collection"]
    return {"context": context, "stage": "core_collection", "input_sha256": request["stageInputs"]["core_collection"],
            "data": Path(ref["path"]).read_bytes(), "sha256": ref["sha256"]}


def test_lost_upload_response_reads_back_once_never_reuploads(tmp_path):
    session = Session(); session.lose_response = True
    client = transport(session)
    receipt = client.upload(**source(tmp_path))
    assert receipt["key"] in session.objects
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert client.evidence()["unconfirmedWriteOutcome"] is False


@pytest.mark.parametrize("failure", ["missing", "unknown", "zero", "unreserved"])
def test_budget_failure_prevents_even_lease_http_or_object_request(tmp_path, failure):
    session, guard = Session(), Guard()
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured())
    values = _task(4).numeric(); values["r2Bytes"] = MAX_OBJECT_BYTES
    cost = _task(0) if failure == "zero" else BudgetVector.estimated(values)
    if failure == "unknown":
        cost = BudgetVector.unknown()
    client = transport(session, guard, budget_ledger=None if failure == "missing" else ledger,
                       budget_task_id="missing-task", request_costs={"POST": cost})
    with pytest.raises(PipelineRunnerError, match="resource_"):
        client.upload(**source(tmp_path))
    assert guard.calls == 0
    assert session.calls == []
    assert client.upload_attempts == 0


def test_ambiguous_upload_does_not_refund_or_bypass_readback_allowance(tmp_path):
    session = Session(); session.lose_response = True
    client = transport(session)
    # Consume all the fixture's r2Bytes allowance in the first POST.
    post = client._request_costs["POST"].numeric()
    post["r2Bytes"] = MAX_OBJECT_BYTES * 12
    client._request_costs["POST"] = BudgetVector.estimated(post)
    with pytest.raises(PipelineRunnerError, match="resource_allowance_rejected"):
        client.upload(**source(tmp_path))
    assert [call[0] for call in session.calls] == ["POST"]
    assert client.unconfirmed_write_outcome is True
    record = client._budget_ledger.snapshot()["reservations"]["fixture-task"]
    assert record["authorizedUpperBound"]["r2Bytes"]["value"] == MAX_OBJECT_BYTES * 12
    assert record["consumed"] is None


def test_two_clients_share_one_atomic_allowance_without_second_http(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    first = transport(Session())
    ledger = first._budget_ledger
    total = _task(100).numeric(); total["r2Bytes"] = MAX_OBJECT_BYTES * 12
    sessions = [Session(), Session()]
    clients = [transport(session, budget_ledger=ledger, budget_task_id="fixture-task",
                         request_costs={"POST": BudgetVector.estimated(total)}) for session in sessions]
    params = source(tmp_path)
    def attempt(client):
        try:
            client.upload(**params)
            return "uploaded"
        except PipelineRunnerError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, clients))
    assert sorted(results) == ["checkpoint_resource_allowance_rejected", "uploaded"]
    assert sum(len(session.calls) for session in sessions) == 1


@pytest.mark.parametrize("status", [302, 401, 409, 429])
def test_rejected_upload_does_not_retry_or_readback(tmp_path, status):
    session = Session(); session.status = status
    client = transport(session)
    with pytest.raises(PipelineRunnerError, match="write_rejected"):
        client.upload(**source(tmp_path))
    assert len(session.calls) == 1
    assert client.evidence()["unconfirmedWriteOutcome"] is True


def test_unconfirmed_write_and_request_budget_fail_closed(tmp_path):
    session = Session(); session.lose_response = True
    client = transport(session, maximum_requests=1)
    with pytest.raises(PipelineRunnerError, match="budget_exceeded"):
        client.upload(**source(tmp_path))
    assert client.unconfirmed_write_outcome
    assert len(session.calls) == 1


def test_corrupt_readback_and_oversize_input_rejected(tmp_path):
    params = source(tmp_path)
    session = Session(); session.lose_response = True; session.corrupt = True
    client = transport(session)
    with pytest.raises(PipelineRunnerError, match="bundle_invalid"):
        client.upload(**params)
    assert client.unconfirmed_write_outcome
    session = Session(); client = transport(session)
    with pytest.raises(PipelineRunnerError, match="object_limit"):
        client.upload(**{**params, "data": b"x" * (MAX_OBJECT_BYTES + 1)})
    assert not session.calls


def test_lease_failure_prevents_http_and_origin_is_restricted(tmp_path):
    session = Session()
    with pytest.raises(RuntimeError, match="lease_lost"):
        transport(session, Guard(fail_at=1)).upload(**source(tmp_path))
    assert not session.calls
    with pytest.raises(PipelineRunnerError, match="origin_invalid"):
        CheckpointObjectTransport(base_url="https://attacker.example", ingest_key="x", access_client_id="x",
                                  access_client_secret="x", lease_guard=Guard())


def test_concurrent_client_calls_share_one_request_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    params = source(tmp_path)
    session = Session()
    client = transport(session, maximum_requests=3)
    def attempt(_):
        try:
            client.upload(**params)
            return True
        except PipelineRunnerError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(20)))
    assert sum(outcomes) == 3
    assert len(session.calls) == client.request_count == 3
