"""Injected publication integration fixtures, not production acceptance."""
import base64
import gzip
import json
from dataclasses import replace

import pytest

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_candidate_transport import CandidatePublicationTransport
from trading_desk.pipeline_publish_adapter import CoreCandidatePublishAdapter
from trading_desk.pipeline_publication_intent import PublicationIntentStore
from trading_desk.pipeline_adapters import PipelineAdapterError
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.resource_budget_remote import SharedBudgetLedger
from test_pipeline_verify_adapter import setup
from test_pipeline_candidate_transport import client
from test_pipeline_bundle_resume import Guard
from test_pipeline_checkpoint_transport import Response
from test_pipeline_bundle_resume import payload, ROOT, LOCK
from trading_desk.pipeline_runner import run_pipeline_request
from datetime import datetime, timezone


class Remote:
    def __init__(self):
        self.calls = []; self.artifacts = {}; self.manifest = None; self.upload_names = []
        self.lost_name = None; self.corrupt = False; self.deny = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url.endswith("/budget"):
            if method == "GET":
                return Response(json.dumps({"revision": 0, "contract": "flow-shared-budget-v1", "rowsRead": 1, "rowsWritten": 0}).encode())
            body = json.loads(kwargs["data"])
            return Response(json.dumps({"decision": "not_authorized" if self.deny else "authorized", "replayed": False,
                "taskId": f'{body["runKey"]}/{body["leaseOwner"]}/{body["attemptCount"]}',
                "revision": 1, "rowsRead": 21, "rowsWritten": 1}).encode())
        if method == "POST":
            body = json.loads(kwargs["data"]); artifact = body["artifact"]
            if body.get("operation") == "inspect_artifact":
                if artifact["name"] not in self.artifacts:
                    return Response(b"{}", 404)
                if self.corrupt:
                    return Response(b"{}", 409)
                return Response(json.dumps({"status": "verified_artifact", "releaseId": body["manifest"]["releaseId"],
                    "asOf": body["manifest"]["asOf"], "manifestSha256": body["manifest"]["manifestSha256"],
                    "name": artifact["name"], "sha256": artifact["sha256"], "bytes": artifact["bytes"]}).encode())
            self.upload_names.append(artifact["name"])
            self.artifacts[artifact["name"]] = gzip.decompress(base64.b64decode(artifact["contentBase64"]))
            if body["finalize"]:
                self.manifest = body["manifest"]
            if self.lost_name == artifact["name"]:
                raise TimeoutError("fixture lost receipt")
            return Response(json.dumps(CandidatePublicationTransport._receipt(body)).encode())
        if self.manifest is None:
            return Response(b"{}", 404)
        name = kwargs["params"]["artifact"]; item = self.manifest["artifacts"][name]
        headers = {"X-Flow-Release-Id": self.manifest["releaseId"], "X-Flow-As-Of": self.manifest["asOf"],
            "X-Flow-Release-Status": "STAGED", "X-Flow-Manifest-Sha256": self.manifest["manifestSha256"],
            "X-Flow-Artifact": name, "X-Flow-Artifact-Sha256": item["sha256"], "X-Flow-Artifact-Bytes": str(item["bytes"])}
        return Response(b"wrong" if self.corrupt else self.artifacts[name], headers=headers)


def fixture(tmp_path):
    verify, params = setup(tmp_path); verified = verify.run(**params)
    context = params["context"]
    source = params["workspace"]; manifest = json.loads((source / "build/manifest.json").read_bytes())
    intent = PublicationIntentStore.initialize(tmp_path / "intent.sqlite", execution=CandidatePublicationTransport.execution(context),
        release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])
    remote = Remote()
    ledger = SharedBudgetLedger(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id",
        access_client_secret="fixture-secret", lease_guard=Guard(), session=remote,
        run_key=context.identity.run_key, lease_owner=context.lease_owner,
        attempt_count=context.lease_attempt_count, target_date=context.identity.target_date)
    transport = client(remote, intent_store=intent, budget_ledger=ledger, budget_task_id=ledger.task_id)
    config = {"verifyInputSha256": params["input_sha256"], "verifyOutputSha256": verified["outputSha256"]}
    adapter = CoreCandidatePublishAdapter(source, transport, context)
    return adapter, dict(context=context, config=config, input_sha256=canonical_sha256(config), workspace=tmp_path / "publish"), remote


def test_exact_stage_publication_and_resume_only_reads(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    result = adapter.run(**params)
    assert result["evidence"]["allArtifactsReadbackVerified"]
    assert result["evidence"]["activeReleaseChanged"] is False
    assert len(remote.artifacts) == 7
    writes = sum(method == "POST" and url.endswith("read-model-import") for method, url in remote.calls)
    assert writes == 7
    # A new process has a new HTTP request ceiling, not a new server allowance.
    # The fixture server authorizes each read explicitly; no quota refund.
    ledger = adapter.transport._ledger
    restarted_ledger = SharedBudgetLedger(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id",
        access_client_secret="fixture-secret", lease_guard=Guard(), session=remote,
        run_key=params["context"].identity.run_key, lease_owner=params["context"].lease_owner,
        attempt_count=params["context"].lease_attempt_count, target_date=params["context"].identity.target_date)
    adapter = replace(adapter, transport=client(remote, intent_store=adapter.transport._intent_store,
        budget_ledger=restarted_ledger, budget_task_id=ledger.task_id))
    assert adapter.run(**params) == result
    assert sum(method == "POST" and url.endswith("read-model-import") for method, url in remote.calls) == writes


def completed_new_host(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    result = adapter.run(**params)
    context = replace(params["context"], lease_owner="next-owner", lease_attempt_count=2)
    manifest = json.loads((adapter.source_dir / "build/manifest.json").read_bytes())
    store = PublicationIntentStore.initialize(tmp_path / "next-host.sqlite",
        execution=CandidatePublicationTransport.execution(context), release_id=manifest["releaseId"],
        manifest_sha256=manifest["manifestSha256"])
    ledger = SharedBudgetLedger(base_url="https://flow.xiaoker.com", ingest_key="fixture-key",
        access_client_id="fixture-id", access_client_secret="fixture-secret", lease_guard=Guard(), session=remote,
        run_key=context.identity.run_key, lease_owner=context.lease_owner,
        attempt_count=context.lease_attempt_count, target_date=context.identity.target_date)
    adapter = replace(adapter, execution_context=context, restore_completed_intents=True,
        transport=client(remote, intent_store=store, budget_ledger=ledger, budget_task_id=ledger.task_id))
    checkpoint = {"identity": context.identity.as_dict(), "stage": "publish", "status": "COMPLETED",
        "inputSha256": params["input_sha256"], "outputSha256": result["outputSha256"]}
    remote.calls.clear()
    return adapter, params, remote, context, checkpoint


def test_completed_checkpoint_new_owner_restores_only_from_staged_reads(tmp_path):
    adapter, params, remote, context, checkpoint = completed_new_host(tmp_path)
    assert adapter.verify_checkpoint(context=context, checkpoint=checkpoint, workspace=params["workspace"])
    assert len(remote.upload_names) == 7
    assert not any(method == "POST" and url.endswith("read-model-import") for method, url in remote.calls)
    manifest, sources = adapter._sources(context, params["config"], params["input_sha256"])
    assert all(adapter.transport.require_durable_intent(body)["status"] == "ACKNOWLEDGED" for _, body in sources)
    assert not adapter.transport._intent_store.has_pending()


@pytest.mark.parametrize("fault", ["corrupt", "missing", "budget", "disabled", "checkpoint"])
def test_completed_handoff_does_not_invent_acknowledgements(tmp_path, fault):
    adapter, params, remote, context, checkpoint = completed_new_host(tmp_path)
    if fault == "corrupt":
        remote.corrupt = True
    elif fault == "missing":
        remote.manifest = None
    elif fault == "budget":
        remote.deny = True
    elif fault == "disabled":
        adapter = replace(adapter, restore_completed_intents=False)
    else:
        checkpoint["outputSha256"] = "0" * 64
    try:
        assert not adapter.verify_checkpoint(context=context, checkpoint=checkpoint, workspace=params["workspace"])
    except PipelineRunnerError:
        pass
    _, sources = adapter._sources(context, params["config"], params["input_sha256"])
    assert all(adapter.transport.require_durable_intent(body) is None for _, body in sources)
    assert len(remote.upload_names) == 7


def test_pending_current_host_intent_cannot_be_blessed_by_checkpoint(tmp_path):
    adapter, params, remote, context, checkpoint = completed_new_host(tmp_path)
    _, sources = adapter._sources(context, params["config"], params["input_sha256"])
    body = sources[0][1]
    adapter.transport._intent_store.begin(adapter.transport._reference(body))
    assert not adapter.verify_checkpoint(context=context, checkpoint=checkpoint, workspace=params["workspace"])
    assert not remote.calls
    assert adapter.transport._intent_store.has_pending()


@pytest.mark.parametrize("fault", ["artifact", "config", "owner", "local_budget", "no_intent"])
def test_invalid_input_has_no_remote_effect(tmp_path, fault):
    adapter, params, remote = fixture(tmp_path)
    if fault == "artifact":
        (adapter.source_dir / "build/overview.json").write_bytes(b"{}")
    elif fault == "config":
        params["input_sha256"] = "0" * 64
    elif fault == "owner":
        params["context"] = replace(params["context"], lease_owner="other")
    elif fault == "local_budget":
        adapter = replace(adapter, transport=client(remote))
    else:
        adapter.transport._intent_store = None
    with pytest.raises((PipelineAdapterError, PipelineRunnerError)):
        adapter.run(**params)
    assert not remote.calls


def test_budget_denial_no_upload(tmp_path):
    adapter, params, remote = fixture(tmp_path); remote.deny = True
    with pytest.raises(PipelineRunnerError, match="allowance_rejected"):
        adapter.run(**params)
    assert not remote.artifacts


def test_unknown_final_can_be_exactly_reconciled(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    names = sorted(json.loads((adapter.source_dir / "build/manifest.json").read_bytes())["artifacts"])
    remote.lost_name = names[-1]
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        adapter.run(**params)
    remote.lost_name = None
    result = adapter.run(**params)
    assert result["status"] == "COMPLETED"
    assert sum(method == "POST" and url.endswith("read-model-import") for method, url in remote.calls) == len(names)


def test_missing_intermediate_readback_never_complete(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    remote.lost_name = sorted(json.loads((adapter.source_dir / "build/manifest.json").read_bytes())["artifacts"])[0]
    with pytest.raises(PipelineRunnerError):
        adapter.run(**params)
    remote.lost_name = None
    remote.artifacts.clear()
    with pytest.raises(PipelineRunnerError, match="intermediate_readback_mismatch"):
        adapter.run(**params)
    assert not (params["workspace"] / "adapter-receipt.json").exists()
    assert adapter.transport.evidence()["unconfirmedWriteOutcome"] is True


def test_unknown_intermediate_resumes_after_exact_inspection(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    name = sorted(json.loads((adapter.source_dir / "build/manifest.json").read_bytes())["artifacts"])[0]
    remote.lost_name = name
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        adapter.run(**params)
    remote.lost_name = None
    manifest = json.loads((adapter.source_dir / "build/manifest.json").read_bytes())
    reopened = PublicationIntentStore(tmp_path / "intent.sqlite", execution=CandidatePublicationTransport.execution(params["context"]),
        release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])
    ledger = adapter.transport._ledger
    adapter = replace(adapter, transport=client(remote, intent_store=reopened, budget_ledger=ledger, budget_task_id=ledger.task_id))
    assert adapter.run(**params)["status"] == "COMPLETED"
    assert adapter.transport.upload_attempts == 6
    assert len(remote.upload_names) == len(set(remote.upload_names)) == 7
    assert adapter.transport.evidence()["unconfirmedWriteOutcome"] is False


def test_corrupt_remote_readback_rejects_local_receipt(tmp_path):
    adapter, params, remote = fixture(tmp_path); result = adapter.run(**params)
    context = params["context"]
    ledger = SharedBudgetLedger(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id",
        access_client_secret="fixture-secret", lease_guard=Guard(), session=remote,
        run_key=context.identity.run_key, lease_owner=context.lease_owner,
        attempt_count=context.lease_attempt_count, target_date=context.identity.target_date)
    adapter = replace(adapter, transport=client(remote, intent_store=adapter.transport._intent_store,
        budget_ledger=ledger, budget_task_id=ledger.task_id))
    remote.corrupt = True
    checkpoint = {"identity": params["context"].identity.as_dict(), "stage": "publish", "status": "COMPLETED",
        "inputSha256": params["input_sha256"], "outputSha256": result["outputSha256"]}
    with pytest.raises(PipelineRunnerError, match="readback_mismatch"):
        adapter.verify_checkpoint(identity=params["context"].identity, checkpoint=checkpoint, workspace=params["workspace"])


def test_existing_runner_invokes_publish_adapter_and_saves_checkpoint(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    request = payload()
    request["identity"]["targetDate"] = params["context"].identity.target_date
    request["stages"] = ["publish"]
    request["stageInputs"] = {"publish": params["input_sha256"]}
    request["stageConfigs"] = {"publish": params["config"]}
    result = run_pipeline_request(request, state_dir=tmp_path / "runner-state", output_path=tmp_path / "runner-result.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
        now=datetime(2026, 10, 3, tzinfo=timezone.utc), stage_adapters={"publish": adapter}, lease_guard=Guard())
    assert result["status"] == "COMPLETED"
    assert len(remote.artifacts) == 7


def test_candidate_effect_rejected_on_nonpublish_stage(tmp_path):
    adapter, params, remote = fixture(tmp_path)
    request = payload(); request["identity"]["targetDate"] = params["context"].identity.target_date
    request["stageInputs"] = {"core_collection": params["input_sha256"]}
    request["stageConfigs"] = {"core_collection": params["config"]}
    with pytest.raises(PipelineRunnerError, match="effect_scope_invalid"):
        run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
            producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
            now=datetime(2026, 10, 3, tzinfo=timezone.utc), stage_adapters={"core_collection": adapter}, lease_guard=Guard())
    assert not remote.calls


@pytest.mark.parametrize("ceiling", ["transport", "control"])
def test_insufficient_request_capacity_rejected_before_any_effect(tmp_path, ceiling):
    adapter, params, remote = fixture(tmp_path)
    if ceiling == "transport":
        adapter.transport._maximum = 1
    else:
        adapter.transport._ledger._maximum = 1
    with pytest.raises(PipelineRunnerError, match="budget_exceeded|control_request_capacity"):
        adapter.run(**params)
    assert not remote.calls
