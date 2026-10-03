"""Candidate client composition fixtures, not production quota evidence."""
import pytest

from trading_desk.resource_budget import BudgetVector
from trading_desk.resource_budget_remote import SharedBudgetLedger
from trading_desk.pipeline_runner import PipelineRunnerError
from test_pipeline_checkpoint_transport import Session as ObjectSession, source, transport, MAX_OBJECT_BYTES
from test_resource_budget_remote import Session as BudgetSession, metadata, receipt, kwargs
from test_resource_budget import _task


def clients(args, *responses, **overrides):
    context = args["context"]
    options = kwargs(session=BudgetSession(*responses))
    options.update(run_key=context.identity.run_key, lease_owner=context.lease_owner,
                   attempt_count=context.lease_attempt_count, target_date=context.identity.target_date)
    options.update(overrides)
    ledger = SharedBudgetLedger(**options)
    for response in responses:
        if isinstance(response.value, dict) and "taskId" in response.value:
            response.value["taskId"] = ledger.task_id
    cost = _task(4).numeric()
    cost["r2Bytes"] = MAX_OBJECT_BYTES
    objects = ObjectSession()
    client = transport(objects, budget_ledger=ledger, budget_task_id=ledger.task_id,
                       request_costs={method: BudgetVector.estimated(cost) for method in ("POST", "GET")})
    return client, ledger, objects


def test_confirmed_shared_grant_precedes_object_write_and_evidence_is_separate(tmp_path):
    args = source(tmp_path)
    client, ledger, objects = clients(args, metadata(), receipt())
    assert client.evidence()["sharedDurableReservation"] is False
    assert ledger.request_count == 0  # Reporting has no hidden requests.
    client.upload(**args)
    assert [call[0] for call in ledger._session.calls] == ["GET", "POST"]
    assert [call[0] for call in objects.calls] == ["POST"]
    evidence = client.evidence()
    assert evidence["resourceAllowance"] == "server_authoritative_shared"
    assert evidence["sharedDurableReservation"] is True
    assert evidence["sharedAuthorizations"] == 1
    assert evidence["budgetControlPlane"]["d1RowsRead"] == 28
    assert evidence["budgetControlPlane"]["d1RowsWritten"] == 1
    assert evidence["d1RowsRead"] is None  # Object endpoint telemetry is not inferred.
    assert evidence["d1RowsWritten"] is None
    assert evidence["accountRemaining"] is None
    assert ledger.request_count == 2


@pytest.mark.parametrize("override", [
    {"run_key": "daily-market-data:2026-10-03:reconcile:v1"},
    {"target_date": "2026-09-28"}, {"lease_owner": "other-owner"}, {"attempt_count": 2},
])
def test_mismatched_lease_identity_never_spends_or_uploads(tmp_path, override):
    args = source(tmp_path)
    client, ledger, objects = clients(args, **override)
    with pytest.raises(PipelineRunnerError, match="shared_budget_identity_mismatch"):
        client.upload(**args)
    assert ledger.request_count == 0 and not objects.calls


@pytest.mark.parametrize("decision,replayed", [("not_authorized", False), ("step_over_budget", False), ("authorized", True)])
def test_denied_or_replayed_budget_never_permits_object_io(tmp_path, decision, replayed):
    args = source(tmp_path)
    client, ledger, objects = clients(args, metadata(), receipt(decision, replayed))
    with pytest.raises(PipelineRunnerError, match="resource_allowance_rejected"):
        client.upload(**args)
    assert ledger.request_count == 2 and not objects.calls
    assert client.evidence()["sharedDurableReservation"] is False


def test_unknown_budget_reply_blocks_object_io_and_preserves_unknown_meta(tmp_path):
    args = source(tmp_path)
    context = args["context"]
    options = kwargs(session=BudgetSession(metadata(), TimeoutError("fixture secret")))
    options.update(run_key=context.identity.run_key, target_date=context.identity.target_date,
                   lease_owner=context.lease_owner, attempt_count=context.lease_attempt_count)
    ledger = SharedBudgetLedger(**options)
    objects = ObjectSession()
    cost = _task(4).numeric(); cost["r2Bytes"] = MAX_OBJECT_BYTES
    client = transport(objects, budget_ledger=ledger, budget_task_id=ledger.task_id,
                       request_costs={"POST": BudgetVector.estimated(cost)})
    with pytest.raises(PipelineRunnerError, match="resource_allowance_rejected"):
        client.upload(**args)
    evidence = client.evidence()
    assert not objects.calls and evidence["budgetControlPlane"]["pendingOperation"] is True
    assert evidence["budgetControlPlane"]["d1RowsRead"] is None
    assert evidence["budgetControlPlane"]["d1RowsWritten"] is None
    with pytest.raises(PipelineRunnerError):
        client.upload(**args)
    assert ledger.request_count == 2 and not objects.calls
