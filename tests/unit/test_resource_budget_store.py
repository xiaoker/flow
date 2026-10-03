from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from trading_desk.resource_budget import BudgetVector
from trading_desk.resource_budget_store import LocalBudgetLedger
from test_resource_budget import _full_limits, _measured, _task

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def _kwargs(clock=lambda: NOW):
    return dict(limits=_full_limits(), measured=_measured(),
                measurement_ttl_seconds=3600, clock=clock)


def test_restart_preserves_allowance_and_idempotent_reservation(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    assert ledger.authorize_step("job", _task(6))["decision"] == "authorized"
    reopened = LocalBudgetLedger(path, **_kwargs())
    assert reopened.reserve("job", _task(10))["decision"] == "idempotent"
    assert reopened.authorize_step("job", _task(6))["decision"] == "step_over_budget"
    assert reopened.snapshot()["reservations"]["job"]["authorizedUpperBound"]["d1RowsRead"]["value"] == 6


def test_independent_processes_cannot_oversell_allowance(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    code = '''
import json,sys
from datetime import datetime,timezone
from test_resource_budget import _full_limits,_measured,_task
from trading_desk.resource_budget_store import LocalBudgetLedger
ledger=LocalBudgetLedger(sys.argv[1],limits=_full_limits(),measured=_measured(),measurement_ttl_seconds=3600,clock=lambda:datetime(2026,10,1,12,tzinfo=timezone.utc))
print(json.dumps(ledger.authorize_step("job",_task(6))))
'''
    env = {**os.environ, "PYTHONPATH": "src:.:tests/unit"}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(20)]
    decisions = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        decisions.append(json.loads(stdout)["decision"])
    assert decisions.count("authorized") == 1
    assert decisions.count("step_over_budget") == 19


def test_unknown_outcome_survives_restart_and_retains_hold(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    ledger.settle("job", outcome="TIMEOUT")
    reopened = LocalBudgetLedger(path, **_kwargs())
    assert reopened.authorize_step("job", _task(1))["decision"] == "not_authorized"
    assert reopened.snapshot()["reserved"]["d1RowsRead"]["value"] == 10


def test_expired_hold_does_not_disappear_on_restart(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    reopened = LocalBudgetLedger(path, **_kwargs(lambda: NOW + timedelta(seconds=1800)))
    assert reopened.authorize_step("job", _task(1))["status"] == "UNKNOWN"
    assert reopened.snapshot()["reserved"]["d1RowsRead"]["value"] == 10


def test_settlement_is_not_counted_twice_after_restart(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    actual = BudgetVector.measured(_task(3).numeric(), measured_at=NOW.isoformat())
    ledger.debit("job", actual)
    first = ledger.settle("job", outcome="SUCCEEDED", actual=actual)
    reopened = LocalBudgetLedger(path, **_kwargs())
    assert reopened.settle("job", outcome="SUCCEEDED", actual=actual) == first
    assert reopened.snapshot()["measured"]["d1RowsRead"]["value"] == _measured().numeric()["d1RowsRead"] + 3


def test_missing_store_and_reinitialization_fail_closed(tmp_path):
    path = tmp_path / "budget.sqlite"
    with pytest.raises(ValueError, match="missing_or_symlink"):
        LocalBudgetLedger(path, **_kwargs())
    LocalBudgetLedger.initialize(path, **_kwargs())
    with pytest.raises(FileExistsError):
        LocalBudgetLedger.initialize(path, **_kwargs())


def test_changed_account_evidence_is_not_a_reset(tmp_path):
    path = tmp_path / "budget.sqlite"
    LocalBudgetLedger.initialize(path, **_kwargs())
    kwargs = {**_kwargs(), "measured": _measured("2026-10-01T12:01:00Z")}
    with pytest.raises(ValueError, match="epoch_conflict"):
        LocalBudgetLedger(path, **kwargs)


@pytest.mark.parametrize("damage", ["UPDATE events SET payload='{}' WHERE sequence=1", "DELETE FROM events WHERE sequence=1"])
def test_corrupt_or_truncated_journal_denies_permission(tmp_path, damage):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    with sqlite3.connect(path) as db:
        db.execute(damage)
    with pytest.raises(ValueError, match="journal_corrupt"):
        ledger.authorize_step("job", _task(1))


def test_capacity_never_resets_held_budget(tmp_path, monkeypatch):
    import trading_desk.resource_budget_store as module
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    with pytest.raises(ValueError, match="journal_capacity"):
        ledger.authorize_step("job", _task(1))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_failed_transition_rolls_back_and_clock_regression_is_rejected(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    with pytest.raises(ValueError, match="conflict"):
        ledger.reserve("job", _task(11))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    reopened = LocalBudgetLedger(path, **_kwargs(lambda: NOW - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="clock_regression"):
        reopened.authorize_step("job", _task(1))


@pytest.mark.parametrize("mutation", ["ttl", "nested_metric"])
def test_mutated_live_configuration_cannot_diverge_from_restart(tmp_path, mutation):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    if mutation == "ttl":
        ledger.reservation_ttl_seconds = 1
    else:
        ledger.measured.metrics["d1RowsRead"] = _task(999).metrics["d1RowsRead"]
    with pytest.raises(ValueError, match="epoch_conflict"):
        ledger.reserve("job", _task(10))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_stale_evidence_after_restart_blocks_new_reservation(tmp_path):
    path = tmp_path / "budget.sqlite"
    LocalBudgetLedger.initialize(path, **_kwargs())
    ledger = LocalBudgetLedger(path, **_kwargs(lambda: NOW + timedelta(seconds=3601)))
    assert ledger.reserve("job", _task(10))["decision"] == "unknown_metrics"


def test_commit_error_rolls_back_event_and_head(tmp_path, monkeypatch):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    original_connect = sqlite3.connect

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("fixture commit failure")

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs:
                        original_connect(*args, **kwargs, factory=FailingCommit))
    with pytest.raises(sqlite3.OperationalError, match="fixture commit failure"):
        ledger.authorize_step("job", _task(6))
    monkeypatch.setattr(sqlite3, "connect", original_connect)
    assert LocalBudgetLedger(path, **_kwargs()).authorize_step("job", _task(6))["decision"] == "authorized"


def test_utf8_event_and_total_bytes_are_bounded(tmp_path, monkeypatch):
    import trading_desk.resource_budget_store as module
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 100)
    with pytest.raises(ValueError, match="event_too_large"):
        ledger.reserve("中文" * 100, _task(10))
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 16_384)
    monkeypatch.setattr(module, "MAX_JOURNAL_BYTES", 100)
    with pytest.raises(ValueError, match="journal_capacity"):
        ledger.reserve("job", _task(10))


def test_abrupt_process_exit_does_not_forget_committed_allowance(tmp_path):
    path = tmp_path / "budget.sqlite"
    ledger = LocalBudgetLedger.initialize(path, **_kwargs())
    ledger.reserve("job", _task(10))
    code = '''
import os,sys
from datetime import datetime,timezone
from test_resource_budget import _full_limits,_measured,_task
from trading_desk.resource_budget_store import LocalBudgetLedger
ledger=LocalBudgetLedger(sys.argv[1],limits=_full_limits(),measured=_measured(),measurement_ttl_seconds=3600,clock=lambda:datetime(2026,10,1,12,tzinfo=timezone.utc))
assert ledger.authorize_step("job",_task(6))["decision"] == "authorized"
os._exit(0)
'''
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            env={**os.environ, "PYTHONPATH": "src:.:tests/unit"}, timeout=30)
    assert result.returncode == 0
    assert LocalBudgetLedger(path, **_kwargs()).authorize_step("job", _task(6))["decision"] == "step_over_budget"
