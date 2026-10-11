"""Client contract fixtures, not production budget or recovery evidence."""
import hashlib
import json
from dataclasses import replace

import pytest

from trading_desk.pipeline_checkpoint_transport import RecoveryCheckpointObjectTransport, MAX_OBJECT_BYTES
from trading_desk.pipeline_checkpoint_bundle import build_stage_bundle
from trading_desk.pipeline_application import build_checkpoint, canonical_sha256
from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
from trading_desk.resource_budget_remote import RecoverySharedBudgetLedger
from trading_desk.resource_budget import BudgetVector
from trading_desk.pipeline_runner import PipelineRunnerError
from test_pipeline_recovery_scope import fixture
from test_pipeline_recovery_lease import Session as LeaseSession
from test_pipeline_lease import Clock
from test_pipeline_checkpoint_transport import Response


class BudgetSession:
    def __init__(self):
        self.calls = []
        self.reject = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, kwargs))
        if method == "GET":
            return Response(json.dumps({"revision": 0, "contract": "flow-shared-budget-v1",
                "rowsRead": None, "rowsWritten": 0}).encode())
        p = json.loads(kwargs["data"])
        task = f'recovery/{p["recoveryId"]}/{p["planSha256"]}/{p["runKey"]}/{p["leaseOwner"]}/{p["attemptCount"]}'
        return Response(json.dumps({"revision": 1, "taskId": task, "decision": "unknown_metrics" if self.reject else "authorized",
            "replayed": False, "rowsRead": None, "rowsWritten": None}).encode())


class ObjectSession:
    def __init__(self):
        self.calls, self.objects = [], {}
        self.lost = False
        self.after_put = lambda: None

    def request(self, method, url, **kwargs):
        self.calls.append((method, kwargs))
        p = kwargs["params"]
        key = f'market-data/pipeline/checkpoints/v1/recovery/{p["recoveryId"]}/{p["scopeSha256"]}/{hashlib.sha256(p["runKey"].encode()).hexdigest()}/{p["producerBuildSha"]}/{p["stage"]}/{p["inputSha256"]}/{p["sha256"]}.json'
        if method == "POST":
            self.objects.setdefault(key, kwargs["data"])
            self.after_put()
            if self.lost:
                raise TimeoutError("fixture lost PUT receipt")
            return Response(json.dumps({"decision": "verified", "key": key, "sha256": p["sha256"], "bytes": len(kwargs["data"])}).encode())
        return Response(self.objects[key], headers={"X-Flow-Checkpoint-Sha256": p["sha256"], "X-Flow-Checkpoint-Key": key})


def setup(tmp_path):
    clock = Clock()
    plan, context = fixture()
    plan.pop("planHash")
    plan["maxRequests"] = 32
    plan["budgetManifest"] = {"runnerBudgetContract": "flow-shared-budget-v1",
        "taskCostUpperBound": BudgetVector.estimated(dict.fromkeys(BudgetVector.unknown().metrics, 10_000_000)).as_dict()}
    plan["planHash"] = canonical_sha256(plan)
    context = replace(context, recovery_plan_sha256=plan["planHash"])
    lease_session = LeaseSession(clock, plan)
    common = dict(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id", access_client_secret="fixture-secret")
    guard = RecoveryPipelineLease(approved_plan=plan, stages=["core_collection"], **common,
        expected_attempt_count=1, session=lease_session, now=clock.now, monotonic=clock.monotonic)
    guard.verify(context)
    budget_session = BudgetSession()
    ledger = RecoverySharedBudgetLedger(approved_plan=plan, context=context, stages=["core_collection"], now=clock.now(),
        **common, run_key=context.identity.run_key, lease_owner=context.lease_owner, attempt_count=1,
        target_date=context.identity.target_date, lease_guard=guard, session=budget_session)
    cost = BudgetVector.estimated(dict.fromkeys(BudgetVector.unknown().metrics, MAX_OBJECT_BYTES))
    objects = ObjectSession()
    client = RecoveryCheckpointObjectTransport(approved_plan=plan, stages=["core_collection"], now=clock.now,
        **common, lease_guard=guard, budget_ledger=ledger, budget_task_id=ledger.task_id,
        request_costs={"POST": cost, "GET": cost}, session=objects)
    workspace = tmp_path / "bundle"
    workspace.mkdir(); (workspace / "snapshot.json").write_text('{"asOf":"2026-09-29"}')
    checkpoint = build_checkpoint(identity=context.identity, stage="core_collection", status="COMPLETED",
        input_sha256="c" * 64, output_sha256="d" * 64)
    data, digest = build_stage_bundle(workspace=workspace, checkpoint=checkpoint,
        identity=context.identity, stage="core_collection", input_sha256="c" * 64)
    args = dict(context=context, stage="core_collection", input_sha256="c" * 64, data=data, sha256=digest)
    return client, args, objects, budget_session, lease_session, clock


def test_exact_recovery_identity_sent_and_lost_receipt_reads_once(tmp_path):
    client, args, objects, budget, lease, clock = setup(tmp_path)
    objects.lost = True
    receipt = client.upload(**args)
    assert [call[0] for call in objects.calls] == ["POST", "GET"]
    assert "/recovery/fixture-recovery/" in receipt["key"]
    assert client.evidence()["unconfirmedWriteOutcome"] is False
    assert client.evidence()["accountRemaining"] is None
    assert client.evidence()["resourceAllowance"] == "server_authoritative_shared"
    for _, call in objects.calls:
        assert call["params"]["planSha256"] == args["context"].recovery_plan_sha256
        assert call["params"]["recoveryId"] == "fixture-recovery"
    assert len(budget.calls) == 4  # two separately bounded allowances; no refund.


@pytest.mark.parametrize("stage", ["handoff", "build", "normalize", "publish"])
def test_unapproved_stage_never_calls_budget_or_object(tmp_path, stage):
    client, args, objects, budget, _, _ = setup(tmp_path)
    with pytest.raises(PipelineRunnerError, match="stage_unproven"):
        client.upload(**{**args, "stage": stage})
    assert not objects.calls and not budget.calls


def test_unknown_shared_budget_never_uploads(tmp_path):
    client, args, objects, budget, _, _ = setup(tmp_path)
    budget.reject = True
    with pytest.raises(PipelineRunnerError, match="allowance_rejected"):
        client.upload(**args)
    assert not objects.calls


def test_revocation_after_put_never_acknowledges_or_reuploads(tmp_path):
    client, args, objects, _, lease, _ = setup(tmp_path)
    objects.after_put = lambda: lease.approval_override.update(status="REVOKED")
    with pytest.raises(PipelineRunnerError):
        client.upload(**args)
    assert [c[0] for c in objects.calls] == ["POST"]
    assert client.unconfirmed_write_outcome
    with pytest.raises(PipelineRunnerError):
        client.upload(**args)
    assert len(objects.calls) == 1


def test_wrong_context_and_understated_plan_read_cost_reject_before_object(tmp_path):
    client, args, objects, budget, _, _ = setup(tmp_path)
    with pytest.raises(PipelineRunnerError, match="scope_rejected"):
        client.upload(**{**args, "context": replace(args["context"], recovery_id="other")})
    assert not budget.calls
    values = client._request_costs["POST"].numeric(); values["r2ClassB"] = 1
    client._request_costs["POST"] = BudgetVector.estimated(values)
    with pytest.raises(PipelineRunnerError, match="understated"):
        client.upload(**args)
    assert not objects.calls and not budget.calls


def test_constructor_requires_real_recovery_guard_and_shared_ledger(tmp_path):
    client, _, _, _, _, _ = setup(tmp_path)
    with pytest.raises(PipelineRunnerError, match="guard_required"):
        RecoveryCheckpointObjectTransport(approved_plan=client._plan, stages=["core_collection"], lease_guard=object())
    with pytest.raises(PipelineRunnerError, match="shared_budget_required"):
        RecoveryCheckpointObjectTransport(approved_plan=client._plan, stages=["core_collection"], lease_guard=client._guard)
