from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_d1_budget import build_report
from trading_desk.resource_budget import ResourceLimits, ResourceUsage, decide, estimate_stage


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
    report = decide(ResourceUsage(), estimate_stage(logical_rows=100), _limits())
    assert report["decision"] == "allow"


def test_optional_stage_is_deferred_above_internal_gate() -> None:
    report = decide(
        ResourceUsage(rows_read=2_400_000, rows_written=49_000),
        estimate_stage(rows_read=200_000, rows_written=2_000),
        _limits(),
        optional=True,
    )
    assert report["decision"] == "defer_optional"


def test_required_stage_is_blocked_above_internal_gate_without_fabrication() -> None:
    report = decide(
        ResourceUsage(rows_read=2_400_000, rows_written=49_000),
        estimate_stage(rows_read=200_000, rows_written=2_000),
        _limits(),
        optional=False,
    )
    assert report["decision"] == "block_d1_bulk"


def test_cli_report_accepts_camel_case_payloads() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text()
    )
    report = build_report(
        config=config,
        measured={"rowsRead": 1_000, "rowsWritten": 2_000},
        predicted={"logicalRows": 100, "indexCount": 2},
    )
    assert report["projected"] == {"rowsRead": 1_100, "rowsWritten": 2_300, "storageBytes": 0}


def test_missing_usage_dimensions_default_to_zero() -> None:
    usage = ResourceUsage.from_mapping({"rowsRead": 100})
    assert usage.rows_read == 100
    assert usage.rows_written == 0
    assert usage.storage_bytes == 0


def test_invalid_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="d1RowsReadPerDay"):
        ResourceLimits.from_mapping({"limits": {"d1RowsReadPerDay": 0}})
