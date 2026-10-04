"""Local transport fixtures, not production or quota acceptance evidence."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trading_desk.pipeline_candidate_transport import CandidatePublicationTransport
from trading_desk.pipeline_runner import PipelineRunnerError, build_execution_context
from trading_desk.read_models import canonical_json_bytes
from trading_desk.resource_budget import BudgetLedger, BudgetVector
from trading_desk.resource_budget_remote import SharedBudgetLedger
from test_pipeline_bundle_resume import Guard, payload
from test_pipeline_checkpoint_transport import Response
from test_resource_budget import _full_limits, _measured, _task


def args():
    request = payload()
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    content = b'{"observed":1.0}'
    manifest = {"schemaVersion": 4, "asOf": "2026-09-29", "releaseId": "fixture-release",
        "producerBuildSha": "a" * 40, "coverage": {"ratio": 1.0}, "artifacts": {"overview": {
            "key": "market-data/releases/2026-09-29/fixture-release/overview.json",
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}}
    manifest["manifestSha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return dict(context=context, manifest_bytes=canonical_json_bytes(manifest), name="overview", content=content)


class Session:
    def __init__(self, *, status=200, lost=False, corrupt=False, receipt=None):
        self.calls = []
        self.status, self.lost, self.corrupt, self.receipt = status, lost, corrupt, receipt
        self.staged = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        if method == "POST":
            body = json.loads(kwargs["data"])
            if body["finalize"]:
                self.staged = body
            if self.lost:
                raise TimeoutError("fixture-private-secret")
            manifest = body["manifest"]
            result = {"status": "staged_candidate" if body["finalize"] else "staged", "asOf": manifest["asOf"]}
            if body["finalize"]:
                result.update(releaseId=manifest["releaseId"], manifestKey=f'market-data/releases/{manifest["asOf"]}/{manifest["releaseId"]}/manifest.json')
            else:
                result["name"] = body["artifact"]["name"]
            return Response(json.dumps(self.receipt if self.receipt is not None else result).encode(), self.status)
        if not self.staged:
            return Response(b"{}", 404)
        manifest = self.staged["manifest"]
        content = args()["content"]
        headers = {"X-Flow-Release-Id": manifest["releaseId"], "X-Flow-As-Of": manifest["asOf"],
            "X-Flow-Release-Status": "STAGED", "X-Flow-Manifest-Sha256": manifest["manifestSha256"],
            "X-Flow-Artifact": "overview", "X-Flow-Artifact-Sha256": hashlib.sha256(content).hexdigest(),
            "X-Flow-Artifact-Bytes": str(len(content))}
        if self.corrupt:
            headers["X-Flow-Manifest-Sha256"] = "b" * 64
        return Response(content, headers=headers)


def client(session, guard=None, **options):
    values = _task(100).numeric(); values.update(r2Bytes=10_000_000)
    ledger = BudgetLedger(limits=replace(_full_limits(), r2_bytes=100_000_000), measured=_measured(),
        clock=lambda: datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    assert ledger.reserve("candidate-task", BudgetVector.estimated(values))["decision"] in {"allow", "warn"}
    costs = _task(20).numeric(); costs.update(r2Bytes=2_000_000)
    defaults = dict(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id",
        access_client_secret="fixture-secret", lease_guard=guard or Guard(), session=session,
        budget_ledger=ledger, budget_task_id="candidate-task",
        request_costs={key: BudgetVector.estimated(costs) for key in ("POST", "GET")})
    return CandidatePublicationTransport(**(defaults | options))


def test_stage_only_wire_exact_bytes_receipt_and_unknown_telemetry():
    session = Session(); transport = client(session); source = args()
    assert transport.upload(**source, finalize=False)["status"] == "staged"
    assert transport.upload(**source, finalize=True)["status"] == "staged_candidate"
    for _, url, request in session.calls:
        assert url.endswith("/api/v1/admin/read-model-import")
        body = json.loads(request["data"])
        assert body["activationMode"] == "stage_only"
        assert body["candidateManifestJson"].encode() == source["manifest_bytes"]
        assert body["pipelineExecution"]["runKey"] == source["context"].identity.run_key
    evidence = transport.evidence()
    assert evidence["d1RowsRead"] is None and evidence["accountRemaining"] is None
    assert evidence["resourceAllowance"] == "in_process_reference"
    assert not evidence["unconfirmedWriteOutcome"]


def test_lost_final_response_only_explicit_exact_readback_no_blind_retry():
    session = Session(lost=True); transport = client(session); source = args()
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        transport.upload(**source, finalize=True)
    with pytest.raises(PipelineRunnerError, match="requires_reconciliation"):
        transport.upload(**source, finalize=True)
    assert len(session.calls) == 1
    result = transport.reconcile_final_upload(**source)
    assert result["status"] == "staged_candidate"
    assert [call[0] for call in session.calls] == ["POST", "GET"]
    assert session.calls[1][2]["params"]["manifestSha256"] == json.loads(source["manifest_bytes"])["manifestSha256"]
    assert not transport.evidence()["unconfirmedWriteOutcome"]
    assert transport.evidence()["bytesReceivedBasis"] == "measured_lower_bound"


def test_lost_intermediate_response_cannot_be_retried_or_read_as_complete():
    session = Session(lost=True); transport = client(session)
    with pytest.raises(PipelineRunnerError):
        transport.upload(**args(), finalize=False)
    with pytest.raises(PipelineRunnerError, match="identity_mismatch"):
        transport.reconcile_final_upload(**args())
    with pytest.raises(PipelineRunnerError, match="requires_reconciliation"):
        transport.upload(**args(), finalize=True)
    assert len(session.calls) == 1 and transport.evidence()["unconfirmedWriteOutcome"]


@pytest.mark.parametrize("status", [302, 401, 409, 429, 503])
def test_http_failure_never_retries_or_refunds(status):
    session = Session(status=status); transport = client(session)
    with pytest.raises(PipelineRunnerError, match="write_rejected"):
        transport.upload(**args(), finalize=True)
    assert len(session.calls) == 1 and transport.evidence()["unconfirmedWriteOutcome"]
    assert transport._ledger.snapshot()["reservations"]["candidate-task"]["consumed"] is None


@pytest.mark.parametrize("receipt", [{}, {"status": "active"}, [], {"status": "staged_candidate", "asOf": "2026-09-28"}])
def test_malformed_or_wrong_receipt_stays_ambiguous(receipt):
    session = Session(receipt=receipt); transport = client(session)
    with pytest.raises(PipelineRunnerError, match="receipt_invalid"):
        transport.upload(**args(), finalize=True)
    assert transport.evidence()["unconfirmedWriteOutcome"]


def test_readback_mismatch_or_missing_candidate_never_clears_hold():
    for corrupt in (False, True):
        session = Session(lost=True, corrupt=corrupt); transport = client(session)
        with pytest.raises(PipelineRunnerError):
            transport.upload(**args(), finalize=True)
        if not corrupt:
            session.staged = None
        with pytest.raises(PipelineRunnerError, match="readback_mismatch"):
            transport.reconcile_final_upload(**args())
        assert transport.evidence()["unconfirmedWriteOutcome"]


@pytest.mark.parametrize("cost", [None, BudgetVector.unknown(), BudgetVector.estimated(_task(0).numeric())])
def test_missing_unknown_understated_budget_prevents_guard_and_http(cost):
    session = Session(); guard = Guard(); transport = client(session, guard, request_costs={"POST": cost})
    with pytest.raises(PipelineRunnerError, match="resource_"):
        transport.upload(**args(), finalize=True)
    assert not session.calls and guard.calls == 0


def test_late_lease_loss_and_request_cap_preserve_unknown():
    session = Session(); transport = client(session, Guard(fail_at=2))
    with pytest.raises(RuntimeError, match="lease_lost"):
        transport.upload(**args(), finalize=True)
    assert transport.evidence()["unconfirmedWriteOutcome"]
    session = Session(lost=True); transport = client(session, maximum_requests=1)
    with pytest.raises(PipelineRunnerError):
        transport.upload(**args(), finalize=True)
    with pytest.raises(PipelineRunnerError, match="budget_exceeded"):
        transport.reconcile_final_upload(**args())
    assert len(session.calls) == 1


def test_shared_grant_and_identity_binding_before_publication():
    from test_resource_budget_remote import Session as BudgetSession, metadata, receipt, kwargs
    source = args(); context = source["context"]
    for mismatch in (False, True):
        responses = (metadata(), receipt())
        opts = kwargs(session=BudgetSession(*responses)) | dict(run_key=context.identity.run_key,
            lease_owner=context.lease_owner, attempt_count=context.lease_attempt_count,
            target_date="2026-09-28" if mismatch else context.identity.target_date)
        ledger = SharedBudgetLedger(**opts)
        for response in responses:
            if isinstance(response.value, dict) and "taskId" in response.value:
                response.value["taskId"] = ledger.task_id
        session = Session(); transport = client(session, budget_ledger=ledger, budget_task_id=ledger.task_id)
        if mismatch:
            with pytest.raises(PipelineRunnerError, match="shared_budget_identity_mismatch"):
                transport.upload(**source, finalize=True)
            assert not ledger.request_count and not session.calls
        else:
            transport.upload(**source, finalize=True)
            assert [call[0] for call in ledger._session.calls] == ["GET", "POST"]
            assert transport.evidence()["sharedAuthorizations"] == 1
            assert transport.evidence()["d1RowsWritten"] is None


def test_origin_credentials_hidden_retries_and_unbound_recovery_rejected():
    for options in ({"base_url": "https://attacker.example"}, {"ingest_key": "x\nsecret"}, {"maximum_requests": True}):
        with pytest.raises(PipelineRunnerError):
            client(Session(), **options)
    session = Session(); session.adapters = {"https": type("Adapter", (), {"max_retries": type("Retry", (), {"total": 1})()})()}
    with pytest.raises(PipelineRunnerError, match="hidden_retries"):
        client(session)
    source = args(); session = Session(); transport = client(session)
    for context in (replace(source["context"], mode="recovery"), replace(source["context"], lease_attempt_count=None)):
        with pytest.raises(PipelineRunnerError, match="ordinary_lease"):
            transport.upload(**(source | {"context": context}), finalize=True)
    assert not session.calls


def test_concurrent_uploads_cannot_exceed_one_client_request_bound():
    from concurrent.futures import ThreadPoolExecutor
    session = Session(); transport = client(session, maximum_requests=1)
    def attempt(_):
        try:
            transport.upload(**args(), finalize=False)
            return True
        except PipelineRunnerError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(attempt, range(20)))
    assert sum(result) == transport.request_count == len(session.calls) == 1


def test_readback_needs_its_own_allowance_and_changed_identity_cannot_clear_hold():
    session = Session(lost=True); transport = client(session, request_costs={"POST": BudgetVector.estimated(
        _task(20).numeric() | {"r2Bytes": 2_000_000})})
    with pytest.raises(PipelineRunnerError):
        transport.upload(**args(), finalize=True)
    source = args()
    with pytest.raises(PipelineRunnerError, match="identity_mismatch"):
        transport.reconcile_final_upload(**(source | {"context": replace(source["context"], lease_owner="late-owner")}))
    with pytest.raises(PipelineRunnerError, match="resource_allowance_required"):
        transport.reconcile_final_upload(**source)
    assert len(session.calls) == 1 and transport.evidence()["unconfirmedWriteOutcome"]


def test_deadline_does_not_allow_late_worker_to_clear_pending(monkeypatch):
    import threading
    import time
    import trading_desk.pipeline_candidate_transport as module
    finish = threading.Event()
    class SlowSession(Session):
        def request(self, method, url, **kwargs):
            response = super().request(method, url, **kwargs)
            if method == "POST":
                finish.wait(2)
            return response
    monkeypatch.setattr(module, "MAX_REQUEST_SECONDS", 0.01)
    session = SlowSession(); transport = client(session)
    started = time.monotonic()
    try:
        with pytest.raises(PipelineRunnerError, match="unavailable"):
            transport.upload(**args(), finalize=True)
        assert time.monotonic() - started < 0.5
        with pytest.raises(PipelineRunnerError, match="still_unconfirmed"):
            transport.reconcile_final_upload(**args())
        assert len(session.calls) == 1
    finally:
        finish.set(); transport._exchange.inflight.join(1)
    assert transport.evidence()["unconfirmedWriteOutcome"]
    transport.reconcile_final_upload(**args())
    assert not transport.evidence()["unconfirmedWriteOutcome"]


def test_oversize_response_closes_and_preserves_unknown(monkeypatch):
    import trading_desk.pipeline_candidate_transport as module
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 2)
    session = Session(); transport = client(session)
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        transport.upload(**args(), finalize=True)
    assert transport.evidence()["unconfirmedWriteOutcome"]


def test_real_loopback_http_uses_existing_auth_and_wire_contract():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append((self.path, self.headers.get("Authorization"), self.headers.get("CF-Access-Client-Id"), data))
            manifest = json.loads(data)["manifest"]
            encoded = json.dumps({"status": "staged_candidate", "asOf": manifest["asOf"], "releaseId": manifest["releaseId"],
                "manifestKey": f'market-data/releases/{manifest["asOf"]}/{manifest["releaseId"]}/manifest.json'}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(encoded))); self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        transport = client(None, base_url=f"http://127.0.0.1:{server.server_port}", allow_loopback_fixture=True)
        transport.upload(**args(), finalize=True)
        path, authorization, access_id, data = captured[0]
        assert path == "/api/v1/admin/read-model-import"
        assert authorization == "Bearer fixture-key" and access_id == "fixture-id"
        assert json.loads(data)["candidateManifestJson"].encode() == args()["manifest_bytes"]
    finally:
        server.shutdown(); server.server_close(); thread.join(1)
