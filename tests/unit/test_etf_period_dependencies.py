from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trading_desk.etf_period_dependencies import (
    OFFICIAL_CALENDAR_SOURCE,
    build_etf_period_rebuild_plan,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _calendar(dates: list[str]) -> dict:
    return {
        "sourceId": OFFICIAL_CALENDAR_SOURCE,
        "sha256": _hash("official-calendar-v1"),
        "datesSha256": _hash_canonical(dates),
        "dates": dates,
    }


def _hash_canonical(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sources(
    dates: list[str],
    *,
    missing: tuple[str, str, str] | None = None,
    nav_counts: dict[tuple[str, str], int] | None = None,
) -> list[dict]:
    result = []
    nav_counts = nav_counts or {}
    for trade_date in dates:
        for market in ("SSE", "SZSE"):
            raw_hash = _hash(f"raw:{trade_date}:{market}")
            normalized_hash = _hash(f"normalized:{trade_date}:{market}")
            normalized_status = (
                "MISSING"
                if missing == (trade_date, market, "normalized")
                else "AVAILABLE"
            )
            raw_status = (
                "MISSING" if missing == (trade_date, market, "raw") else "AVAILABLE"
            )
            result.append(
                {
                    "tradeDate": trade_date,
                    "market": market,
                    "raw": {
                        "status": raw_status,
                        "sha256": raw_hash if raw_status == "AVAILABLE" else None,
                        "bytes": 128 if raw_status == "AVAILABLE" else None,
                    },
                    "normalized": {
                        "status": normalized_status,
                        "sha256": normalized_hash if normalized_status == "AVAILABLE" else None,
                        "sourceSha256": raw_hash if normalized_status == "AVAILABLE" else None,
                        "bytes": 256 if normalized_status == "AVAILABLE" else None,
                        "rowCount": 4 if normalized_status == "AVAILABLE" else None,
                        "shareRows": 4 if normalized_status == "AVAILABLE" else None,
                        "navAlignedRows": nav_counts.get((trade_date, market), 4)
                        if normalized_status == "AVAILABLE"
                        else None,
                    },
                }
            )
    return result


def _plan(
    dates: list[str],
    *,
    targets: list[str],
    sources: list[dict] | None = None,
    models: list[dict] | None = None,
    periods: tuple[str, ...] = ("1d", "3d"),
    classification_version: str = "classification-test-v1",
) -> dict:
    return build_etf_period_rebuild_plan(
        calendar=_calendar(dates),
        target_dates=targets,
        source_evidence=sources if sources is not None else _sources(dates),
        existing_models=models or [],
        classification_version=classification_version,
        periods=periods,
    )


def test_period_windows_use_official_sessions_and_exact_preceding_baseline() -> None:
    dates = [
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
    ]

    plan = _plan(dates, targets=["2026-09-03"], periods=("3d",))
    window = plan["windows"][0]

    assert window["baselineDate"] == "2026-08-31"
    assert window["snapshotDates"] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
    ]
    assert window["dailyChangeDates"] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert "2026-08-29" not in window["snapshotDates"]
    assert "2026-08-30" not in window["snapshotDates"]


def test_ytd_includes_real_prior_session_baseline_without_calendar_day_guessing() -> None:
    dates = [
        "2025-12-31",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]

    plan = _plan(dates, targets=["2026-01-07"], periods=("ytd",))
    window = plan["windows"][0]

    assert window["baselineDate"] == "2025-12-31"
    assert window["snapshotDates"] == dates
    assert window["dailyChangeDates"] == dates[1:]


def test_missing_normalized_source_names_exact_market_date_and_recovery_stage() -> None:
    dates = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]
    plan = _plan(
        dates,
        targets=["2026-09-03"],
        sources=_sources(
            dates,
            missing=("2026-09-01", "SZSE", "normalized"),
        ),
        periods=("3d",),
    )

    gap = next(item for item in plan["sourceGaps"] if item["market"] == "SZSE")
    assert gap == {
        "tradeDate": "2026-09-01",
        "market": "SZSE",
        "stage": "normalized",
        "status": "MISSING",
        "reasonCode": "normalized_fact_not_available",
        "action": "NORMALIZE_FROM_ARCHIVED_RAW",
    }
    assert plan["planStatus"] == "BLOCKED"
    assert plan["executionStatus"] == "NOT_RUN"
    assert plan["resourceEstimate"]["futureBuildCostStatus"] == "UNKNOWN"


def test_missing_same_date_nav_blocks_amount_but_not_share_dependency() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    sources = _sources(dates, nav_counts={("2026-09-01", "SSE"): 0})
    plan = _plan(
        dates,
        targets=["2026-09-01"],
        sources=sources,
        periods=("1d",),
    )

    shares = [item for item in plan["dailyChangeNodes"] if item["metric"] == "shares"]
    amount = [item for item in plan["dailyChangeNodes"] if item["metric"] == "estimatedAmount"]
    assert all(item["decision"] == "BUILD" for item in shares)
    assert all(item["decision"] == "BLOCKED" for item in amount)
    assert any(
        gap["tradeDate"] == "2026-09-01"
        and gap["market"] == "SSE"
        and gap["stage"] == "normalized_nav"
        for gap in plan["sourceGaps"]
    )


def test_partial_nav_is_a_partial_build_not_a_fabricated_complete_value() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    plan = _plan(
        dates,
        targets=["2026-09-01"],
        sources=_sources(dates, nav_counts={("2026-09-01", "SSE"): 2}),
        periods=("1d",),
    )

    amount_nodes = [item for item in plan["dailyChangeNodes"] if item["metric"] == "estimatedAmount"]
    assert all(item["dependencyStatus"] == "PARTIAL" for item in amount_nodes)
    assert all(item["decision"] == "BUILD_PARTIAL" for item in amount_nodes)
    assert all(item["reasonCode"] == "same_date_nav_coverage_incomplete" for item in amount_nodes)
    gap = next(item for item in plan["sourceGaps"] if item["status"] == "PARTIAL")
    assert (gap["availableRows"], gap["expectedRows"]) == (2, 4)


def test_lineage_hash_mismatch_blocks_without_rebuilding_from_untrusted_normalized_data() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    sources = _sources(dates)
    sources[0]["normalized"]["sourceSha256"] = _hash("different-source")

    plan = _plan(dates, targets=["2026-09-01"], sources=sources, periods=("1d",))

    assert plan["planStatus"] == "BLOCKED"
    assert any(item["reasonCode"] == "normalized_source_hash_mismatch" for item in plan["sourceGaps"])
    assert all(item["decision"] == "BLOCKED" for item in plan["dailyChangeNodes"])


def test_changed_old_source_rebuilds_only_downstream_dependencies() -> None:
    dates = [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-06",
        "2026-07-07",
    ]
    targets = ["2026-07-06", "2026-07-07"]
    original_sources = _sources(dates)
    original = _plan(dates, targets=targets, sources=original_sources)
    existing_models = [
        {
            "kind": node["kind"],
            "asOf": node["asOf"],
            "period": node.get("period"),
            "metric": node["metric"],
            "sampleScope": node["sampleScope"],
            "status": node["dependencyStatus"],
            "inputFingerprint": node["inputFingerprint"],
        }
        for node in original["dailyChangeNodes"] + original["periodNodes"]
        if node["dependencyStatus"] in {"READY", "PARTIAL"} and node["inputFingerprint"] is not None
    ]
    changed_sources = [dict(row) for row in original_sources]
    for index, row in enumerate(changed_sources):
        row = {**row, "raw": dict(row["raw"]), "normalized": dict(row["normalized"])}
        changed_sources[index] = row
        if row["tradeDate"] == "2026-07-01" and row["market"] == "SSE":
            row["raw"]["sha256"] = _hash("revised-raw:2026-07-01:SSE")
            row["normalized"]["sourceSha256"] = row["raw"]["sha256"]
    revised = _plan(
        dates,
        targets=targets,
        sources=changed_sources,
        models=existing_models,
    )

    rebuilt_daily = {
        (item["asOf"], item["metric"], item["sampleScope"])
        for item in revised["dailyChangeNodes"]
        if item["decision"] in {"BUILD", "BUILD_PARTIAL"}
    }
    assert {item[0] for item in rebuilt_daily} == {"2026-07-02"}
    stale_3d = [
        item
        for item in revised["periodNodes"]
        if item["period"] == "3d" and item["decision"] in {"BUILD", "BUILD_PARTIAL"}
    ]
    assert {item["asOf"] for item in stale_3d} == {"2026-07-06"}
    assert all(
        item["decision"] == "REUSE"
        for item in revised["periodNodes"]
        if item["asOf"] == "2026-07-07"
    )


def test_unrelated_calendar_revision_changes_plan_hash_not_unaffected_model_fingerprints() -> None:
    dates = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07"]
    first = _plan(dates, targets=["2026-07-07"], periods=("1d",))
    models = [
        {
            "kind": node["kind"],
            "asOf": node["asOf"],
            "period": node.get("period"),
            "metric": node["metric"],
            "sampleScope": node["sampleScope"],
            "status": node["dependencyStatus"],
            "inputFingerprint": node["inputFingerprint"],
        }
        for node in first["dailyChangeNodes"] + first["periodNodes"]
    ]
    revised_dates = ["2026-06-30", *dates]
    revised = _plan(
        revised_dates,
        targets=["2026-07-07"],
        sources=_sources(revised_dates),
        models=models,
        periods=("1d",),
    )

    assert first["planHash"] != revised["planHash"]
    assert all(item["decision"] == "REUSE" for item in revised["dailyChangeNodes"])
    assert all(item["decision"] == "REUSE" for item in revised["periodNodes"])


def test_formula_or_classification_version_change_invalidates_only_models_not_raw_facts() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    original = _plan(dates, targets=["2026-09-01"], periods=("1d",))
    models = [
        {
            "kind": node["kind"],
            "asOf": node["asOf"],
            "period": node.get("period"),
            "metric": node["metric"],
            "sampleScope": node["sampleScope"],
            "status": node["dependencyStatus"],
            "inputFingerprint": node["inputFingerprint"],
        }
        for node in original["dailyChangeNodes"] + original["periodNodes"]
        if node["inputFingerprint"] is not None
    ]

    unchanged = _plan(
        dates,
        targets=["2026-09-01"],
        periods=("1d",),
        models=models,
    )
    new_formulas = {
        "shares": "sum_of_daily_official_share_changes_v2",
        "estimatedAmount": "sum_of_daily_share_changes_times_same_day_nav_v2",
    }
    formula_plan = build_etf_period_rebuild_plan(
        calendar=_calendar(dates),
        target_dates=["2026-09-01"],
        source_evidence=_sources(dates),
        existing_models=models,
        classification_version="classification-test-v1",
        formula_versions=new_formulas,
        periods=("1d",),
    )
    class_plan = _plan(
        dates,
        targets=["2026-09-01"],
        periods=("1d",),
        models=models,
        classification_version="classification-test-v2",
    )

    assert all(item["decision"] == "REUSE" for item in unchanged["dailyChangeNodes"])
    assert all(item["decision"] == "BUILD" for item in formula_plan["dailyChangeNodes"])
    assert all(item["decision"] == "BUILD" for item in class_plan["periodNodes"])
    assert len(formula_plan["sourceGaps"]) == 0
    assert formula_plan["resourceEstimate"]["futureBuildCostStatus"] == "UNKNOWN"


def test_plan_hash_is_stable_under_input_order_and_does_not_infer_missing_calendar_dates() -> None:
    dates = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"]
    evidence = _sources(dates)
    left = _plan(dates, targets=["2026-09-02"], sources=evidence, periods=("3d",))
    right = _plan(
        dates,
        targets=["2026-09-02"],
        sources=list(reversed(evidence)),
        periods=("3d",),
    )

    assert left["planHash"] == right["planHash"]
    assert left["windows"][0]["snapshotDates"] == dates
    assert all(len(item["planHash"]) == 64 for item in (left, right))


def test_insufficient_calendar_is_blocked_instead_of_guessing_a_baseline() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    plan = _plan(dates, targets=["2026-09-01"], periods=("3d",))

    assert plan["windows"][0]["status"] == "BLOCKED_CALENDAR"
    assert plan["windows"][0]["reasonCode"] == "official_calendar_baseline_missing"
    assert plan["windows"][0]["requiredSnapshotCount"] == 4
    assert "baselineDate" not in plan["windows"][0]
    assert plan["planStatus"] == "BLOCKED"


def test_rejects_duplicate_invalid_and_unbounded_calendar_inputs() -> None:
    dates = ["2026-08-31", "2026-09-01"]
    with pytest.raises(ValueError, match="target_dates_duplicate"):
        _plan(dates, targets=["2026-09-01", "2026-09-01"], periods=("1d",))
    with pytest.raises(ValueError, match="target_date_not_in_official_calendar"):
        _plan(dates, targets=["2026-09-02"], periods=("1d",))
    with pytest.raises(ValueError, match="official_calendar_source_invalid"):
        build_etf_period_rebuild_plan(
            calendar={"sourceId": "weekday_guess", "sha256": _hash("x"), "dates": dates},
            target_dates=["2026-09-01"],
            source_evidence=_sources(dates),
            existing_models=[],
            periods=("1d",),
        )
    with pytest.raises(ValueError, match="official_calendar_limit_exceeded"):
        build_etf_period_rebuild_plan(
            calendar={
                "sourceId": OFFICIAL_CALENDAR_SOURCE,
                "sha256": _hash("x"),
                "datesSha256": _hash_canonical([f"2025-01-{day:02d}" for day in range(1, 32)] * 18),
                "dates": [f"2025-01-{day:02d}" for day in range(1, 32)] * 18,
            },
            target_dates=["2025-01-01"],
            source_evidence=[],
            existing_models=[],
        )


def test_offline_cli_writes_plan_only_and_reports_future_cost_unknown(tmp_path: Path) -> None:
    dates = ["2026-08-31", "2026-09-01"]
    input_path = tmp_path / "evidence.json"
    output_path = tmp_path / "plan.json"
    input_path.write_text(
        json.dumps(
            {
                "calendar": _calendar(dates),
                "targetDates": ["2026-09-01"],
                "sourceEvidence": _sources(dates),
                "existingModels": [],
                "periods": ["1d"],
            }
        ),
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/plan_etf_period_rebuild.py"),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": "src:."},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(output_path.read_text(encoding="utf-8"))
    assert plan["executionStatus"] == "NOT_RUN"
    assert plan["planStatus"] == "PLAN_READY"
    assert plan["resourceEstimate"]["plannerD1RowsRead"] == 0
    assert plan["resourceEstimate"]["plannerR2Gets"] == 0
    assert plan["resourceEstimate"]["futureBuildCostStatus"] == "UNKNOWN"
