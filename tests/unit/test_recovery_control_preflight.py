"""Offline envelopes are diagnostics, never a budget grant or lease bypass."""
import pytest
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.pipeline_checkpoint_preflight import recovery_control_preflight, pipeline_task_preflight
from test_pipeline_task_preflight import task_arguments
from test_pipeline_recovery_checkpoint import setup


def arguments(**changes):
    return dict(checkpoint_requests=2, ledger_operations=0, runner_confirmations=0,
        renewal_upper_bound=0, **changes)


def test_exact_fixture_operation_count_and_no_extra_remote_calls(tmp_path):
    client, args, objects, budget, lease, _ = setup(tmp_path)
    receipt = client.upload(**args)
    client.download(context=args["context"], stage=args["stage"],
        input_sha256=args["input_sha256"], sha256=receipt["sha256"])
    report = recovery_control_preflight(**arguments())
    assert report["leaseRequestsUpperBound"] == len(lease.calls) == 26
    assert report["workerRequestsUpperBound"] == len(lease.calls) + len(budget.calls) + len(objects.calls) == 32
    assert report["status"] == "DECLARED_ENVELOPE_CHECKED"
    assert report["liveExecutionAllowed"] is False
    report = recovery_control_preflight(**dict(arguments(), checkpoint_requests=3))
    assert report["status"] == "BLOCKED"
    assert report["leaseRequestsUpperBound"] == 38
    assert report["accountRemaining"] == report["providerRows"] == "UNKNOWN"


@pytest.mark.parametrize("name", ["checkpoint_requests", "ledger_operations", "runner_confirmations", "renewal_upper_bound"])
def test_missing_or_invalid_counts_fail_closed(name):
    value = dict(arguments(), **{name: None})
    report = recovery_control_preflight(**value)
    assert report["status"] == "BLOCKED" and report["leaseRequestsUpperBound"] is None
    for bad in (True, -1, 1.5, 10001):
        with pytest.raises(ValueError):
            recovery_control_preflight(**dict(arguments(), **{name: bad}))


def test_approval_cap_and_lifecycle_costs_are_not_ignored():
    report = recovery_control_preflight(**dict(arguments(), ledger_operations=3,
        runner_confirmations=2, renewal_upper_bound=1))
    assert report["leaseRequestsUpperBound"] == 57 and report["status"] == "BLOCKED"
    assert recovery_control_preflight(**arguments(approved_request_limit=20))["status"] == "BLOCKED"
    with pytest.raises(ValueError):
        recovery_control_preflight(**arguments(lease_request_limit=33))


def test_server_fenced_budget_still_counts_transport_and_renewals(tmp_path):
    client, args, objects, budget, lease, _ = setup(tmp_path, server_fenced_checks=True)
    receipt = client.upload(**args)
    client.download(context=args["context"], stage=args["stage"],
        input_sha256=args["input_sha256"], sha256=receipt["sha256"])
    report = recovery_control_preflight(**arguments(), server_fenced_checks=True)
    assert report["leaseRequestsUpperBound"] == len(lease.calls) == 10
    assert report["workerRequestsUpperBound"] == 16
    assert report["liveExecutionAllowed"] is False
    bounded = recovery_control_preflight(**dict(arguments(), ledger_operations=3,
        runner_confirmations=2, renewal_upper_bound=1), server_fenced_checks=True)
    assert bounded["leaseRequestsUpperBound"] == 17
    assert bounded["workerRequestsUpperBound"] == 29


def test_candidate_budget_check_renews_and_rejects_revoked_approval(tmp_path):
    client, args, objects, budget, lease, clock = setup(tmp_path, server_fenced_checks=True)
    clock.seconds = 180
    lease.approval_override = {"status": "REVOKED"}
    with pytest.raises(PipelineRunnerError, match="checkpoint_resource_allowance_rejected"):
        client.upload(**args)
    with pytest.raises(PipelineRunnerError, match="recovery_lease_approval_conflict"):
        client._guard.check()
    assert not objects.calls and not budget.calls
    assert [call[0] for call in lease.calls] == ["GET", "GET", "POST", "GET"]


def test_existing_task_report_consumes_control_blockers(tmp_path):
    args = task_arguments(tmp_path)
    assert pipeline_task_preflight(**args)["status"] == "LOCAL_INPUTS_CHECKED"
    args["recovery_control"] = dict(arguments(), checkpoint_requests=3)
    report = pipeline_task_preflight(**args)
    assert report["status"] == "BLOCKED"
    assert "recovery_lease_request_envelope_exceeded" in report["blockers"]
    assert "recovery_control_worker_cost_understated" in report["blockers"]
