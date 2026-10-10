import json
import threading
from datetime import timedelta

import pytest

from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.recovery_execution import plan_bytes
from test_pipeline_lease import Clock, Response, body, context
from test_pipeline_recovery_scope import fixture


class Session:
    def __init__(self, clock, plan):
        self.clock, self.plan, self.calls = clock, plan, []
        self.approval_override, self.current_override, self.receipt_override = {}, {}, {}
        self.status = 200

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        current = {**body(self.clock), "recoveryId": "fixture-recovery", "status": "RUNNING"}
        if method == "POST":
            payload = {**current, "recoveryStatus": "ACTIVE", **self.receipt_override}
        elif url.endswith("/recovery"):
            scope_sha = plan_bytes(self.plan)[1]
            payload = {"recoveryId": "fixture-recovery", "runKey": current["runKey"],
                "planSha256": self.plan["planHash"], "scopeSha256": scope_sha,
                "planKey": f"market-data/recovery/plans/fixture-recovery-{scope_sha}.json",
                "status": "ACTIVE", "expiresAt": "2026-09-30T10:00:00Z",
                "pipeline": current, **self.approval_override}
        else:
            payload = {**current, **self.current_override}
        return Response(payload, self.status)


def setup(**kwargs):
    clock = Clock()
    plan, bound = fixture()
    session = Session(clock, plan)
    lease = RecoveryPipelineLease(approved_plan=plan, stages=["core_collection"],
        base_url="https://flow.xiaoker.com", ingest_key="fixture-secret", access_client_id="fixture-id",
        access_client_secret="fixture-secret", expected_attempt_count=1, session=session,
        now=clock.now, monotonic=clock.monotonic, **kwargs)
    return clock, bound, session, lease


def test_authority_then_renewal_requires_fresh_full_approval():
    clock, bound, session, lease = setup()
    assert lease.verify(bound)
    assert len(session.calls) == 2
    clock.seconds = 180
    lease.check()
    assert [call[0] for call in session.calls] == ["GET", "GET", "POST", "GET", "GET"]
    sent = session.calls[2][2]["json"]
    assert sent == {"recoveryId": "fixture-recovery", "runKey": bound.identity.run_key,
        "leaseOwner": bound.lease_owner, "expectedAttemptCount": 1, "status": "RUNNING", "renewLease": True}
    assert lease.evidence()["rowsWritten"] == 3
    assert lease.evidence()["accountRemainingBudget"]["basis"] == "unknown"
    assert "fixture-secret" not in json.dumps(lease.evidence())


@pytest.mark.parametrize("override", [{"status": "REVOKED"}, {"status": "PAUSED"},
    {"status": "COMPLETED"}, {"planSha256": "0" * 64}, {"scopeSha256": "0" * 64},
    {"planKey": "other"}, {"runKey": "other"}, {"recoveryId": "other"},
    {"expiresAt": "2026-09-30T08:59:00Z"}, {"expiresAt": "2026-09-30T10:00:00"}])
def test_approval_mismatch_stops_without_effects(override):
    clock, bound, session, lease = setup()
    session.approval_override = override
    with pytest.raises(PipelineRunnerError):
        lease.verify(bound)
    count = len(session.calls)
    with pytest.raises(PipelineRunnerError):
        lease.check()
    assert len(session.calls) == count
    assert all(call[0] == "GET" for call in session.calls)


@pytest.mark.parametrize("override", [{"leaseOwner": "other"}, {"attemptCount": 2},
    {"attemptCount": True}, {"recoveryId": None}, {"targetDate": "2026-09-30"},
    {"windowDate": "2026-09-29"}, {"status": "COMPLETED"}])
def test_current_row_fences_owner_attempt_and_data_date(override):
    clock, bound, session, lease = setup()
    session.current_override = override
    with pytest.raises(PipelineRunnerError):
        lease.verify(bound)


def test_revoke_after_successful_renewal_prevents_next_effect():
    clock, bound, session, lease = setup()
    lease.verify(bound)
    clock.seconds = 180
    session.approval_override = {"status": "REVOKED"}
    with pytest.raises(PipelineRunnerError, match="approval_conflict"):
        lease.check()
    assert lease.renewal_count == 1
    count = len(session.calls)
    with pytest.raises(PipelineRunnerError):
        lease.confirm()
    assert len(session.calls) == count


def test_budget_reserved_before_write_and_no_expired_renewal():
    clock, bound, session, lease = setup(maximum_requests=4)
    lease.verify(bound)
    clock.seconds = 180
    with pytest.raises(PipelineRunnerError, match="budget_exceeded"):
        lease.check()
    assert len(session.calls) == 2
    clock, bound, session, lease = setup()
    lease.verify(bound)
    clock.seconds = 901
    with pytest.raises(PipelineRunnerError, match="expired"):
        lease.check()
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [302, 401, 403, 409, 429, 503])
def test_http_rejection_retains_ambiguous_write_no_retry(status):
    clock, bound, session, lease = setup()
    lease.verify(bound)
    clock.seconds = 180
    session.status = status
    with pytest.raises(PipelineRunnerError):
        lease.check()
    assert lease.evidence()["unconfirmedWriteOutcome"]
    assert len(session.calls) == 3


def test_ordinary_context_cannot_use_recovery_guard():
    clock, bound, session, lease = setup()
    with pytest.raises(PipelineRunnerError, match="scope_rejected"):
        lease.verify(context())
    assert not session.calls


def test_confirm_rechecks_authority_and_shorter_approval_is_execution_deadline():
    clock, bound, session, lease = setup()
    session.approval_override = {"expiresAt": (clock.now() + timedelta(seconds=60)).isoformat()}
    lease.verify(bound)
    session.current_override = {"attemptCount": 2}
    with pytest.raises(PipelineRunnerError):
        lease.confirm()
    assert len(session.calls) == 4


def test_plan_mutation_after_constructor_does_not_change_scope():
    clock, bound, session, lease = setup()
    session.plan["requiredStages"].append("publish")
    with pytest.raises(PipelineRunnerError):
        lease.verify(bound)


def test_concurrent_boundaries_share_one_renewal():
    clock, bound, session, lease = setup()
    lease.verify(bound)
    clock.seconds = 180
    errors = []
    def check():
        try:
            lease.check()
        except Exception as error:
            errors.append(error)
    threads = [threading.Thread(target=check) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert not errors
    assert len(session.calls) == 5
    assert lease.renewal_count == 1


@pytest.mark.parametrize("override", [{"rowsWritten": 0}, {"rowsWritten": True},
    {"rowsWritten": 33}, {"recoveryStatus": "PAUSED"}, {"recoveryId": "other"}])
def test_invalid_renewal_receipt_does_not_grant_permission(override):
    clock, bound, session, lease = setup()
    lease.verify(bound)
    session.receipt_override = override
    clock.seconds = 180
    with pytest.raises(PipelineRunnerError, match="renewal_receipt_invalid"):
        lease.check()
    assert lease.evidence()["unconfirmedWriteOutcome"]
    assert lease.renewal_count == 0
    assert len(session.calls) == 3
