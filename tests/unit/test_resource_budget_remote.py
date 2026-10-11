from __future__ import annotations

import json
import os
import subprocess
from datetime import timezone
from datetime import datetime
from pathlib import Path

import pytest

from trading_desk.resource_budget import BudgetLedger, BudgetVector, RESOURCE_DIMENSIONS, ResourceLimits
from trading_desk.resource_budget_remote import SharedBudgetLedger, RecoverySharedBudgetLedger

ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-10-03T02:00:00Z"
KEY = "daily-market-data:2026-10-03:reconcile:v1"


def vector(value, measured=False):
    values = dict.fromkeys(RESOURCE_DIMENSIONS, value)
    return BudgetVector.measured(values, measured_at=NOW) if measured else BudgetVector.estimated(values)


class Guard:
    def __init__(self):
        self.calls = 0

    def confirm(self):
        self.calls += 1


class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code, self.closed = value, status, False

    def iter_content(self, **kwargs):
        yield self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def kwargs(**extra):
    return dict(base_url="https://flow.xiaoker.com", ingest_key="fixture-only", access_client_id="fixture-id",
                access_client_secret="fixture-secret", run_key=KEY, lease_owner="owner", attempt_count=1,
                target_date="2026-10-02", lease_guard=Guard(), **extra)


def metadata():
    return Response({"revision": 0, "contract": "flow-shared-budget-v1", "rowsRead": 7, "rowsWritten": 0})


def receipt(decision="authorized", replayed=False):
    return Response({"decision": decision, "replayed": replayed, "rowsRead": 21, "rowsWritten": 1,
                     "revision": 1, "taskId": f"{KEY}/owner/1"})


def recovery_fixture(manifest=None):
    from trading_desk.recovery_execution import build_recovery_plan
    from trading_desk.pipeline_application import build_application_identity
    from trading_desk.pipeline_runner import PipelineExecutionContext
    plan = build_recovery_plan(campaign_id="fixture", recovery_id="fixture-recovery", run_key=KEY,
        data_dates=["2026-10-02"], required_stages=["core_collection"], artifact_root=".recovery/fixture",
        state_dir=".recovery/fixture/state", max_dates=1, max_requests=20, deadline="2026-10-03T03:00:00Z",
        budget_manifest=manifest or {"runnerBudgetContract": "flow-shared-budget-v1", "taskCostUpperBound": vector(80).as_dict()})
    context = PipelineExecutionContext(identity=build_application_identity(run_key=KEY, window_date="2026-10-03",
        target_date="2026-10-02", producer_build_sha="a" * 40), mode="recovery", lease_owner="owner",
        lease_expires_at="2026-10-03T03:00:00Z", authorization_kind="approved-recovery",
        recovery_id="fixture-recovery", recovery_plan_sha256=plan["planHash"], dependency_lock_sha256="b" * 64,
        lease_attempt_count=1)
    return plan, context


def recovery_options(plan, context):
    return dict(approved_plan=plan, context=context, stages=["core_collection"],
        now=datetime(2026, 10, 3, 2, tzinfo=timezone.utc))


def test_recovery_cross_language_real_http_and_sqlite_admission():
    plan, context = recovery_fixture()
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/shared-budget-server.mjs")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "FLOW_RECOVERY_FIXTURE_PLAN": json.dumps(plan)})
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), process.stderr.read() if process.poll() is not None else "fixture failed to start"
        options = kwargs(allow_loopback_fixture=True)
        options["base_url"] = f"http://127.0.0.1:{port}"
        ledger = RecoverySharedBudgetLedger(**options, **recovery_options(plan, context))
        assert ledger.reserve(ledger.task_id, vector(70))["decision"] == "allow"
        assert ledger.authorize_step(ledger.task_id, vector(10))["decision"] == "authorized"
        assert ledger.accounting_evidence()["accountRemaining"] is None
        assert ledger.accounting_evidence()["d1RowsWritten"] is None
        ordinary = SharedBudgetLedger(**options)
        with pytest.raises(ValueError, match="allowance_not_granted"):
            ordinary.reserve(ordinary.task_id, vector(1))
        ledger.validate_execution_identity(run_key=KEY, lease_owner="owner", attempt_count=1,
            target_date="2026-10-02", task_id=ledger.task_id,
            recovery_id=context.recovery_id, recovery_plan_sha256=context.recovery_plan_sha256)
        with pytest.raises(ValueError, match="identity_mismatch"):
            ledger.validate_execution_identity(run_key=KEY, lease_owner="owner", attempt_count=1,
                target_date="2026-10-02", task_id=ledger.task_id)
    finally:
        process.terminate()
        process.communicate(timeout=5)


@pytest.mark.parametrize("manifest", [{"fixture": True}, {"runnerBudgetContract": "flow-shared-budget-v1", "taskCostUpperBound": vector(80).as_dict() | {"r2Bytes": {"value": None, "unit": "bytes", "basis": "unknown"}}}])
def test_recovery_missing_or_unknown_upper_bound_rejected_before_http(manifest):
    plan, context = recovery_fixture(manifest)
    session = Session()
    with pytest.raises(ValueError):
        RecoverySharedBudgetLedger(**kwargs(session=session), **recovery_options(plan, context))
    assert not session.calls


def test_recovery_scope_overrun_rejected_locally_without_http():
    plan, context = recovery_fixture()
    session = Session()
    ledger = RecoverySharedBudgetLedger(**kwargs(session=session), **recovery_options(plan, context))
    with pytest.raises(ValueError, match="scope_over_budget"):
        ledger.reserve(ledger.task_id, vector(81))
    assert not session.calls


def test_same_fixture_matches_existing_budget_ledger():
    fixture = json.loads((ROOT / "tests/fixtures/shared-budget-trace.json").read_text())
    limits = ResourceLimits.from_mapping({"limits": {"d1RowsReadPerDay": 100, "d1RowsWrittenPerDay": 100,
        "workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100, "r2ClassA": 100, "r2ClassB": 100},
        "targets": {"d1RowsReadPerDay": 90, "d1RowsWrittenPerDay": 90},
        "hardLimits": {"d1RowsReadPerDay": 100, "d1RowsWrittenPerDay": 100},
        "storage": {"d1Bytes": 100, "hardD1Bytes": 100, "r2SoftBytes": 100}})
    ledger = BudgetLedger(limits=limits, measured=vector(0, True), measurement_ttl_seconds=3600,
                          clock=lambda: datetime.fromisoformat(fixture["now"].replace("Z", "+00:00")))
    for step in fixture["trace"]:
        op = step["operation"]
        if op == "settle":
            result = ledger.settle("task", outcome=step["outcome"], actual=vector(step["value"], True) if "value" in step else None)
        else:
            result = getattr(ledger, op)("task", *([vector(step["value"], step.get("measured", False))] if "value" in step else []))
        assert (result["decision"], result["status"]) == (step["decision"], step["status"])
    assert ledger.snapshot()["measured"]["d1RowsRead"]["value"] == 8


def test_cross_language_real_http_and_sqlite_admission():
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/shared-budget-server.mjs")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), process.stderr.read() if process.poll() is not None else "fixture failed to start"
        options = kwargs(allow_loopback_fixture=True)
        options["base_url"] = f"http://127.0.0.1:{port}"
        first = SharedBudgetLedger(**options)
        assert first.reserve(first.task_id, vector(30))["decision"] == "allow"
        assert first.authorize_step(first.task_id, vector(20))["decision"] == "authorized"
        second = SharedBudgetLedger(**options)
        assert second.authorize_step(second.task_id, vector(20))["decision"] == "step_over_budget"
        assert second.settle(second.task_id, outcome="TIMEOUT")["decision"] == "unknown_outcome"
        assert first.authorize_step(first.task_id, vector(1))["decision"] == "not_authorized"
        assert first.rows_read is None and first.rows_written is None
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_permission_committed_before_return_and_http_is_bounded():
    session = Session(metadata(), receipt())
    ledger = SharedBudgetLedger(**kwargs(session=session))
    assert ledger.authorize_step(ledger.task_id, vector(2))["decision"] == "authorized"
    assert ledger.rows_read == 28 and ledger.rows_written == 1
    assert ledger.request_count == 2
    for _, url, call in session.calls:
        assert url.endswith("/pipeline-window/budget")
        assert call["timeout"] == (5, 10) and call["allow_redirects"] is False and call["stream"] is True
    body = json.loads(session.calls[1][2]["data"])
    assert len(body["operationId"]) == 64 and body["targetDate"] == "2026-10-02"


def test_lost_reply_sticky_unknown_and_explicit_same_id_reconciliation():
    session = Session(metadata(), TimeoutError("secret should not leak"), receipt(replayed=True))
    ledger = SharedBudgetLedger(**kwargs(session=session))
    with pytest.raises(ValueError, match="allowance_not_granted") as error:
        ledger.authorize_step(ledger.task_id, vector(2))
    assert "secret" not in str(error.value)
    assert ledger.rows_read is None and ledger.rows_written is None
    with pytest.raises(ValueError, match="outcome_unknown"):
        ledger.authorize_step(ledger.task_id, vector(2))
    result = ledger.resolve_pending()
    assert result["decision"] == "not_authorized"
    assert session.calls[1][2]["data"] == session.calls[2][2]["data"]
    assert ledger.request_count == 3  # No automatic retry on timeout.
    assert ledger.rows_read is None and ledger.rows_written is None


def test_slow_response_has_foreground_deadline_and_no_late_permission(monkeypatch):
    import threading
    import time
    import trading_desk.resource_budget_remote as module
    finish = threading.Event()
    class Slow(Response):
        def iter_content(self, **kwargs):
            finish.wait(timeout=2)
            yield json.dumps(self.value).encode()
    monkeypatch.setattr(module, "MAX_REQUEST_SECONDS", 0.01)
    reply = Slow(receipt().value)
    session = Session(metadata(), reply)
    ledger = SharedBudgetLedger(**kwargs(session=session))
    started = time.monotonic()
    with pytest.raises(ValueError, match="allowance_not_granted"):
        ledger.authorize_step(ledger.task_id, vector(1))
    assert time.monotonic() - started < 0.5
    assert ledger.rows_read is None and ledger.rows_written is None
    with pytest.raises(ValueError, match="still_unconfirmed"):
        ledger.resolve_pending()
    assert len(session.calls) == 2
    finish.set()
    ledger._inflight.join(timeout=1)
    assert reply.closed and ledger._pending is not None


@pytest.mark.parametrize("reply", [receipt(replayed=True), Response(b"[]"), Response(b"x" * 20000), Response({"error": "db"}, 503)])
def test_invalid_or_replayed_grants_never_authorize(reply):
    ledger = SharedBudgetLedger(**kwargs(session=Session(metadata(), reply)))
    with pytest.raises(ValueError):
        ledger.authorize_step(ledger.task_id, vector(1))
    assert reply.closed


def test_revision_conflict_does_not_create_blind_retry():
    session = Session(metadata(), Response({"error": "budget_revision_conflict"}, 409))
    ledger = SharedBudgetLedger(**kwargs(session=session))
    with pytest.raises(ValueError):
        ledger.reserve(ledger.task_id, vector(2))
    assert len(session.calls) == 2 and ledger._pending is None


def test_task_and_request_cap_reject_before_network():
    session = Session(metadata())
    ledger = SharedBudgetLedger(**kwargs(session=session, maximum_requests=1))
    with pytest.raises(ValueError, match="task_mismatch"):
        ledger.reserve("other", vector(1))
    assert not session.calls
    ledger.snapshot()
    with pytest.raises(ValueError, match="request_limit"):
        ledger.snapshot()
    assert len(session.calls) == 1


def test_fresh_late_authorization_reconciliation_still_grants_no_io():
    session = Session(metadata(), TimeoutError(), receipt(replayed=False))
    ledger = SharedBudgetLedger(**kwargs(session=session))
    with pytest.raises(ValueError):
        ledger.authorize_step(ledger.task_id, vector(1))
    assert ledger.resolve_pending()["decision"] == "not_authorized"
    assert ledger.rows_read is None and ledger.rows_written is None


@pytest.mark.parametrize("field,value", [("base_url", "https://evil.example"), ("ingest_key", "x\ny"),
    ("attempt_count", True), ("target_date", "2026-02-30"), ("lease_owner", "owner?"), ("maximum_requests", 33)])
def test_invalid_configuration(field, value):
    options = kwargs()
    options[field] = value
    with pytest.raises(ValueError):
        SharedBudgetLedger(**options)
