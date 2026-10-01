from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.check_d1_budget import build_report
from scripts.check_resource_budget import build_admission_report
from trading_desk.resource_budget import (
    BudgetLedger,
    BudgetVector,
    ResourceLimits,
    ResourceUsage,
    decide,
    estimate_stage,
    evaluate_budget,
)


def _limits() -> ResourceLimits:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    return ResourceLimits.from_mapping(config)


def test_index_write_estimate_is_explicit() -> None:
    estimate = estimate_stage(logical_rows=100, index_count=2)
    assert estimate.rows_read == 100
    assert estimate.rows_written == 300


def test_budget_inside_target_is_allowed() -> None:
    report = decide(
        ResourceUsage(rows_read=0, rows_written=0, storage_bytes=0),
        estimate_stage(logical_rows=100),
        _limits(),
    )
    assert report["decision"] == "allow"


def test_optional_stage_is_deferred_above_internal_gate() -> None:
    report = decide(
        ResourceUsage(rows_read=2_400_000, rows_written=49_000, storage_bytes=0),
        estimate_stage(rows_read=200_000, rows_written=2_000),
        ResourceLimits.from_mapping(
            {
                **json.loads((Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()),
                "storage": {"d1Bytes": 5_000_000_000, "hardD1Bytes": 2_500_000_000},
            }
        ),
        optional=True,
    )
    assert report["decision"] == "defer_optional"


def test_required_stage_is_blocked_above_internal_gate_without_fabrication() -> None:
    report = decide(
        ResourceUsage(rows_read=2_400_000, rows_written=49_000, storage_bytes=0),
        estimate_stage(rows_read=200_000, rows_written=2_000),
        ResourceLimits.from_mapping(
            {
                **json.loads((Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()),
                "storage": {"d1Bytes": 5_000_000_000, "hardD1Bytes": 2_500_000_000},
            }
        ),
        optional=False,
    )
    assert report["decision"] == "block_d1_bulk"


def test_cli_report_accepts_camel_case_payloads() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    report = build_report(
        config=config,
        measured={"rowsRead": 1_000, "rowsWritten": 2_000, "storageBytes": 0},
        predicted={"logicalRows": 100, "indexCount": 2},
    )
    assert report["projected"] == {"rowsRead": 1_100, "rowsWritten": 2_300, "storageBytes": 0}


def test_missing_usage_dimensions_remain_unknown() -> None:
    usage = ResourceUsage.from_mapping({"rowsRead": 100})
    assert usage.rows_read == 100
    assert usage.rows_written is None
    assert usage.storage_bytes is None
    assert ResourceUsage.from_mapping({"rowsRead": "unknown"}).rows_read is None


def test_explicit_unknown_vector_value_is_not_coerced_to_zero() -> None:
    vector = BudgetVector.from_mapping(
        {"d1RowsRead": "unknown"}, default_basis="estimated", scope="fixture"
    )
    assert vector.metrics["d1RowsRead"].value is None
    assert vector.metrics["d1RowsRead"].basis == "unknown"


def _full_limits() -> ResourceLimits:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    config["limits"].update(
        {"r2ClassA": 100, "r2ClassB": 100, "workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100}
    )
    config["targets"].update({"workersRequestsPerDay": 80, "githubActionsMinutesPerMonth": 80})
    config["hardLimits"].update({"workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100})
    return ResourceLimits.from_mapping(config)


def _measured(at: str = "2026-10-01T12:00:00Z") -> BudgetVector:
    return BudgetVector.measured(
        {dimension: 0 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")},
        measured_at=at,
    )


def _task(cost: int = 10) -> BudgetVector:
    return BudgetVector.estimated(
        {dimension: cost for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")}
    )


def test_unknown_account_or_task_metric_fails_closed_without_zero_fabrication() -> None:
    unknown = BudgetVector.unknown(scope="account")
    report = evaluate_budget(
        limits=_full_limits(), measured=unknown, reserved=_task(0), task_cost=_task(1)
    )
    assert report["decision"] == "unknown_metrics"
    assert "account.d1RowsRead" in report["unknownMetrics"]
    assert report["measured"]["d1RowsRead"]["value"] is None


def test_stale_measurement_times_out_and_does_not_admit_task() -> None:
    report = evaluate_budget(
        limits=_full_limits(),
        measured=_measured("2026-10-01T10:00:00Z"),
        reserved=_task(0),
        task_cost=_task(1),
        now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        measurement_ttl_seconds=60,
    )
    assert report["decision"] == "unknown_metrics"
    assert "account.d1RowsRead" in report["unknownMetrics"]


def test_atomic_reservation_prevents_concurrent_oversell() -> None:
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured(), reservation_ttl_seconds=300)
    task = _task(60)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda index: ledger.reserve(f"task-{index}", task), range(20)))
    acquired = [result for result in results if result["decision"] in {"allow", "warn"}]
    assert len(acquired) == 1
    assert sum(result["decision"] == "over_budget" for result in results) == 19


def test_unknown_settlement_retains_reservation_and_is_idempotent() -> None:
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured(), reservation_ttl_seconds=300)
    assert ledger.reserve("job-1", _task(10))["decision"] == "allow"
    first = ledger.settle("job-1", outcome="TIMEOUT")
    second = ledger.settle("job-1", outcome="TIMEOUT")
    assert first == second
    assert first["status"] == "UNKNOWN"
    assert ledger.snapshot()["reserved"]["d1RowsRead"]["value"] == 10


def test_unknown_settlement_can_be_reconciled_by_late_measured_receipt() -> None:
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured(), reservation_ttl_seconds=300)
    assert ledger.reserve("job-1", _task(10))["decision"] == "allow"
    unknown = ledger.settle("job-1", outcome="TIMEOUT")
    assert unknown["status"] == "UNKNOWN"
    actual = BudgetVector.measured(
        {dimension: 4 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")},
        measured_at="2026-10-01T12:00:00Z",
        scope="job-1-late-receipt",
    )
    reconciled = ledger.settle("job-1", outcome="SUCCEEDED", actual=actual)
    assert reconciled["decision"] == "settled"
    assert reconciled["status"] == "SUCCEEDED"
    assert ledger.snapshot()["reserved"]["d1RowsRead"]["value"] == 0
    assert ledger.snapshot()["measured"]["d1RowsRead"]["value"] == 4


def test_debit_keeps_physical_rows_and_marks_upper_bound_overrun() -> None:
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured(), reservation_ttl_seconds=300)
    assert ledger.reserve("job-physical", _task(10))["decision"] == "allow"
    physical_delta = BudgetVector.measured(
        {dimension: 3 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")},
        measured_at="2026-10-01T12:00:00Z",
        scope="d1-meta",
    )
    debited = ledger.debit("job-physical", physical_delta)
    assert debited["decision"] == "debited"
    assert debited["consumed"]["d1RowsRead"]["value"] == 3
    too_many = BudgetVector.measured(
        {dimension: 8 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")},
        measured_at="2026-10-01T12:00:00Z",
        scope="d1-meta",
    )
    overrun = ledger.debit("job-physical", too_many)
    assert overrun["decision"] == "over_budget"
    assert overrun["overBudget"]["d1RowsRead"] == {"consumed": 11, "reserved": 10}


def test_expired_reservation_becomes_unknown_and_remains_reserved() -> None:
    now = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc)]
    ledger = BudgetLedger(
        limits=_full_limits(),
        measured=_measured(),
        reservation_ttl_seconds=60,
        clock=lambda: now[0],
    )
    assert ledger.reserve("job-expiring", _task(10))["decision"] == "allow"
    now[0] = datetime(2026, 10, 1, 12, 2, tzinfo=timezone.utc)
    assert ledger.expire() == ("job-expiring",)
    snapshot = ledger.snapshot()
    assert snapshot["reservations"]["job-expiring"]["status"] == "UNKNOWN"
    assert snapshot["reserved"]["d1RowsRead"]["value"] == 10


def test_measured_settlement_moves_actual_usage_into_future_admission() -> None:
    ledger = BudgetLedger(limits=_full_limits(), measured=_measured(), reservation_ttl_seconds=300)
    assert ledger.reserve("job-1", _task(60))["decision"] == "allow"
    actual = BudgetVector.measured(
        {dimension: 40 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")},
        measured_at="2026-10-01T12:00:00Z",
        scope="job-1",
    )
    result = ledger.settle("job-1", outcome="SUCCEEDED", actual=actual)
    assert result["decision"] == "settled"
    assert ledger.snapshot()["measured"]["d1RowsRead"]["value"] == 40
    # The remaining conservative capacity is 60, so another 61-unit task is
    # rejected instead of being admitted on the old pre-settlement zero.
    assert ledger.reserve("job-2", _task(61))["decision"] == "over_budget"


def test_full_admission_cli_contract_keeps_basis_and_physical_rows() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    config["limits"].update({"r2ClassA": 100, "r2ClassB": 100, "workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100})
    config["hardLimits"].update({"workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100})
    measured = {"measuredAt": "2026-10-01T12:00:00Z", **{dimension: 0 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")}}
    zero = {dimension: 0 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")}
    task = {dimension: 1 for dimension in zero}
    report = build_admission_report(config=config, measured=measured, reserved=zero, task=task, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
    assert report["decision"] == "allow"
    assert report["measured"]["d1RowsRead"]["basis"] == "measured"
    assert report["taskCostUpperBound"]["d1RowsRead"]["basis"] == "estimated"
    assert report["projected"]["d1RowsRead"] == 1


def test_full_admission_unknown_r2_limit_is_not_treated_as_zero() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    zero = {dimension: 0 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")}
    report = build_admission_report(
        config=config,
        measured={"measuredAt": "2026-10-01T12:00:00Z", **zero},
        reserved=zero,
        task={dimension: 1 for dimension in zero},
        now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )
    assert report["decision"] == "unknown_metrics"
    assert "limit.r2ClassA" in report["unknownMetrics"]


def test_full_admission_script_is_a_real_offline_fail_closed_entrypoint(tmp_path: Path) -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    config["limits"].update({"r2ClassA": 100, "r2ClassB": 100, "workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100})
    config["hardLimits"].update({"workersRequestsPerDay": 100, "githubActionsMinutesPerMonth": 100})
    zero = {dimension: 0 for dimension in ("d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB")}
    measured = {"measuredAt": "2026-10-01T12:00:00Z", **zero}
    reserved = dict(zero)
    task = {dimension: 1 for dimension in zero}
    paths = {name: tmp_path / f"{name}.json" for name in ("config", "measured", "reserved", "task")}
    paths["config"].write_text(json.dumps(config), encoding="utf-8")
    paths["measured"].write_text(json.dumps(measured), encoding="utf-8")
    paths["reserved"].write_text(json.dumps(reserved), encoding="utf-8")
    paths["task"].write_text(json.dumps(task), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_resource_budget.py",
            "--config",
            str(paths["config"]),
            "--measured",
            str(paths["measured"]),
            "--reserved",
            str(paths["reserved"]),
            "--task",
            str(paths["task"]),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": "src:."},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "allow"


def test_invalid_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="d1RowsReadPerDay"):
        ResourceLimits.from_mapping({"limits": {"d1RowsReadPerDay": 0}})
