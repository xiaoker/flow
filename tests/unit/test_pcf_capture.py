from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.pcf_capture import (
    PcfCaptureJobContract,
    PcfCaptureItemContract,
    PcfManifestContract,
    build_changed_etf_capture_plan,
)


def _row(code: str, shares: object, *, market: str = "SSE", name: str | None = None) -> dict[str, object]:
    return {
        "market": market,
        "instrument_code": code,
        "instrument_name": name or f"ETF {code}",
        "shares": shares,
    }


def test_outer_join_plan_keeps_all_baseline_states_and_is_deterministic() -> None:
    current = [
        _row("510300", 120),
        _row("510500", 100),
        _row("510800", 50),
    ]
    previous = [
        _row("510300", 100),
        _row("510500", 100),
        _row("510900", 90),
    ]

    plan = build_changed_etf_capture_plan(
        current,
        previous,
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )

    assert [item["etfCode"] for item in plan["items"]] == ["510300", "510500", "510800", "510900"]
    assert {item["baselineStatus"] for item in plan["items"]} == {
        "COMPARABLE_CHANGED",
        "COMPARABLE_UNCHANGED",
        "NEW_BASELINE_MISSING",
        "MISSING_CURRENT",
    }
    assert plan["counts"] == {
        "expected": 4,
        "required": 1,
        "unchanged": 1,
        "newBaselineMissing": 1,
        "missingCurrent": 1,
        "invalidShare": 0,
    }
    assert plan["items"][0]["state"] == "PENDING"
    assert plan["items"][1]["state"] == "NOT_REQUIRED"

    repeat = build_changed_etf_capture_plan(
        pd.DataFrame(current),
        pd.DataFrame(previous),
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )
    assert repeat["planSha256"] == plan["planSha256"]
    assert json.dumps(repeat, ensure_ascii=False, sort_keys=True) == json.dumps(
        plan, ensure_ascii=False, sort_keys=True
    )


def test_plan_does_not_silently_truncate_801_changed_etfs() -> None:
    current = [_row(f"{100000 + index:06d}", index + 1) for index in range(801)]
    previous = [_row(f"{100000 + index:06d}", index) for index in range(801)]

    plan = build_changed_etf_capture_plan(
        current,
        previous,
        target_date="2026-08-21",
        previous_date="2026-08-20",
        hard_limit=800,
    )

    assert plan["status"] == "BLOCKED_UNIVERSE_ANOMALY"
    assert plan["counts"]["required"] == 801
    assert len(plan["items"]) == 801


def test_invalid_share_is_explicit_and_never_required() -> None:
    plan = build_changed_etf_capture_plan(
        [_row("510300", "not-a-number")],
        [_row("510300", 100)],
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )

    assert plan["status"] == "BLOCKED_QUALITY"
    assert plan["counts"]["invalidShare"] == 1
    assert plan["items"][0]["baselineStatus"] == "INVALID_SHARE"
    assert plan["items"][0]["required"] is False


def test_job_and_item_contracts_reject_inconsistent_ready_state() -> None:
    digest = hashlib.sha256(b"pcf").hexdigest()
    with pytest.raises(ValueError):
        PcfCaptureJobContract(
            jobId="job-1",
            targetDate="2026-08-21",
            previousDate="2026-08-20",
            planSha256=digest,
            calculationVersion="pcf-capture-v2",
            status="READY",
            expectedCount=1,
            requiredCount=1,
            unchangedCount=0,
            newBaselineCount=0,
            missingCurrentCount=0,
            capturedCount=1,
            validCount=0,
            failedCount=0,
            startedAt="2026-08-21T00:00:00Z",
            updatedAt="2026-08-21T00:00:00Z",
        )

    with pytest.raises(ValueError):
        PcfCaptureItemContract(
            jobId="job-1",
            etfCode="510300",
            etfName="ETF",
            currentShares=100,
            previousShares=100,
            shareDelta=0,
            baselineStatus="COMPARABLE_CHANGED",
            state="PENDING",
            updatedAt="2026-08-21T00:00:00Z",
        )


def test_manifest_contract_requires_exact_coverage_for_ready_artifacts() -> None:
    digest = hashlib.sha256(b"pcf").hexdigest()
    manifest = PcfManifestContract(
        schemaVersion=2,
        dataset="sse_etf_pcf",
        calculationVersion="pcf-capture-v2",
        tradeDate="2026-08-21",
        previousDate="2026-08-20",
        jobId="job-1",
        planSha256=digest,
        universe={"required": 1, "unchanged": 0, "newBaselineMissing": 0, "missingCurrent": 0},
        coverage={"captured": 1, "valid": 1, "failed": 0, "ppm": 1_000_000},
        artifacts={"headers": {"key": "headers.csv.gz", "sha256": digest, "rows": 1}},
        quality={"pass": 1, "warn": 0, "fail": 0},
        observedAt="2026-08-21T00:00:00Z",
    )
    assert manifest.model_dump(by_alias=True)["schemaVersion"] == 2


def test_pr0_fixtures_cover_date_cash_unknown_and_malicious_xml() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "pcf"
    names = {path.name for path in fixture_root.glob("*.xml")}
    assert names == {
        "same-day.xml",
        "next-day-rolled-over.xml",
        "zero-quantity-cash-substitution.xml",
        "unknown-component-tag.xml",
        "malicious-dtd.xml",
    }
    assert "<!DOCTYPE" in (fixture_root / "malicious-dtd.xml").read_text(encoding="utf-8")
