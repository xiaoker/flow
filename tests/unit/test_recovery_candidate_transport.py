from dataclasses import replace
import pytest
from trading_desk.pipeline_candidate_transport import RecoveryCandidatePublicationTransport
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
from trading_desk.resource_budget_remote import RecoverySharedBudgetLedger
from trading_desk.resource_budget import BudgetVector
from trading_desk.pipeline_application import canonical_sha256
from test_pipeline_recovery_scope import fixture, NOW
from test_pipeline_recovery_lease import Session as LeaseSession
from test_pipeline_recovery_checkpoint import BudgetSession
from test_pipeline_lease import Clock
from test_pipeline_candidate_transport import args, Session

def setup():
    clock = Clock(); plan, context = fixture(("publish",))
    plan.pop("planHash")
    plan["maxRequests"] = 32
    plan["budgetManifest"] = {"runnerBudgetContract": "flow-shared-budget-v1",
        "taskCostUpperBound": BudgetVector.estimated(dict.fromkeys(BudgetVector.unknown().metrics, 10_000_000)).as_dict()}
    plan["planHash"] = canonical_sha256(plan)
    context = replace(context, recovery_plan_sha256=plan["planHash"])
    common = dict(base_url="https://flow.xiaoker.com", ingest_key="fixture-key", access_client_id="fixture-id", access_client_secret="fixture-secret")
    lease = LeaseSession(clock, plan)
    guard = RecoveryPipelineLease(approved_plan=plan, stages=["publish"], **common,
        expected_attempt_count=1, session=lease, now=clock.now, monotonic=clock.monotonic)
    guard.verify(context)
    budget = BudgetSession()
    ledger = RecoverySharedBudgetLedger(approved_plan=plan, context=context, stages=["publish"], now=NOW,
        **common, run_key=context.identity.run_key, lease_owner=context.lease_owner, attempt_count=1,
        target_date=context.identity.target_date, lease_guard=guard, session=budget)
    objects = Session(); cost = BudgetVector.estimated(dict.fromkeys(BudgetVector.unknown().metrics, 2_000_000))
    client = RecoveryCandidatePublicationTransport(approved_plan=plan, now=clock.now, **common,
        lease_guard=guard, budget_ledger=ledger, budget_task_id=ledger.task_id,
        request_costs={"POST": cost, "GET": cost}, session=objects)
    return client, {**args(), "context": context, "finalize": True}, objects, budget, lease

def test_recovery_identity_and_stage_only_wire():
    client, source, objects, budget, _ = setup()
    assert client.upload(**source)["status"] == "staged_candidate"
    body = objects.staged
    assert body["activationMode"] == "stage_only"
    assert body["pipelineExecution"]["recoveryId"] == source["context"].recovery_id
    assert client.evidence()["accountRemaining"] is None
    assert len(budget.calls) == 2

def test_unknown_budget_and_wrong_scope_never_upload():
    client, source, objects, budget, _ = setup()
    with pytest.raises(PipelineRunnerError, match="scope_rejected"):
        client.upload(**{**source, "context": replace(source["context"], recovery_id="other")})
    budget.reject = True
    with pytest.raises(PipelineRunnerError, match="allowance_rejected"):
        client.upload(**source)
    assert not objects.calls

def test_lost_upload_is_not_blindly_repeated():
    client, source, objects, _, _ = setup(); objects.lost = True
    with pytest.raises(PipelineRunnerError, match="unavailable"):
        client.upload(**source)
    with pytest.raises(PipelineRunnerError, match="unconfirmed"):
        client.upload(**source)
    objects.lost = False
    assert client.reconcile_final_upload(**{k: v for k, v in source.items() if k != "finalize"})["status"] == "staged_candidate"
    assert [call[0] for call in objects.calls] == ["POST", "GET"]
