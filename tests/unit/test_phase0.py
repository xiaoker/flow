from __future__ import annotations

from trading_desk.models import QualityFinding
from trading_desk.phase0 import (
    DEFAULT_PCF_MAX_CODES,
    _deferred_datasets_for_window,
    _pending_required_coverage_datasets,
    _resolve_phase0_gate,
)


def _coverage(dataset: str, status: str = "warn") -> QualityFinding:
    return QualityFinding(
        check_id="timeliness.requested_date_coverage",
        dataset=dataset,
        status=status,  # type: ignore[arg-type]
        severity="high",
        message="未覆盖全部请求日期",
        evidence={"missing_dates": ["2026-08-18"]},
    )


def test_source_date_gap_is_pending_but_schema_failure_is_not() -> None:
    findings = [_coverage("szse_etf"), _coverage("sse_etf_nav")]

    assert _pending_required_coverage_datasets(findings) == ["szse_etf"]
    assert _resolve_phase0_gate(
        {"counts": {"fail": 0, "warn": 2, "pass": 0}, "findings": [item.to_dict() for item in findings]},
        [],
        ["szse_etf"],
    ) == "pending"

    assert _resolve_phase0_gate(
        {"counts": {"fail": 0, "warn": 1, "pass": 0}, "findings": []},
        [{"error_type": "HTTPError"}],
        [],
    ) == "blocked"


def test_same_window_without_gaps_can_pass_or_warn_conditionally() -> None:
    assert _resolve_phase0_gate(
        {
            "counts": {"fail": 0, "warn": 0, "pass": 1},
            "findings": [],
        },
        [],
        [],
    ) == "pass"
    assert _resolve_phase0_gate(
        {
            "counts": {"fail": 0, "warn": 1, "pass": 1},
            "findings": [{"status": "warn", "severity": "high"}],
        },
        [],
        [],
    ) == "conditional_pass"


def test_daily_pcf_enrichment_has_a_bounded_default() -> None:
    assert 0 < DEFAULT_PCF_MAX_CODES <= 32


def test_collection_windows_defer_only_late_szse_sources() -> None:
    assert _deferred_datasets_for_window("evening") == ["szse_etf", "szse_margin"]
    assert _deferred_datasets_for_window("morning") == []
    assert _deferred_datasets_for_window("reconcile") == []
    # Manual/legacy invocations keep the complete-source behaviour.
    assert _deferred_datasets_for_window("auto") == []
