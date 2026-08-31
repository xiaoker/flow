from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.reconcile_etf_daily_changes import _candidate_dates
from trading_desk.etf_daily_changes import (
    build_official_etf_daily_changes,
    official_etf_daily_changes_fingerprint,
    reconcile_official_etf_daily_changes,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures/etf_daily_changes"


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # One complete adjacent pair.
            {"trade_date": "2026-08-21", "market": "SSE", "instrument_code": "510300", "shares": 100},
            {"trade_date": "2026-08-22", "market": "SSE", "instrument_code": "510300", "shares": 130},
            # New listing on the as-of date.
            {"trade_date": "2026-08-22", "market": "SSE", "instrument_code": "510500", "shares": 50},
            # A source gap: the code was seen before the expected previous date.
            {"trade_date": "2026-08-19", "market": "SSE", "instrument_code": "510999", "shares": 40},
            {"trade_date": "2026-08-22", "market": "SSE", "instrument_code": "510999", "shares": 60},
            # Present on the previous date but absent now (delisted/removed).
            {"trade_date": "2026-08-21", "market": "SSE", "instrument_code": "510050", "shares": 80},
            # SZSE has not published the as-of date, so its market status is delayed.
            {"trade_date": "2026-08-21", "market": "SZSE", "instrument_code": "159919", "shares": 200},
        ]
    )


def test_build_contract_explicitly_marks_alignment_states() -> None:
    contract = build_official_etf_daily_changes(
        _frame(),
        as_of="2026-08-22",
        source_release_ids=["etf_scale:sse:2026-08-22:abc123", "etf_scale:szse:2026-08-21:def456"],
    )

    rows = {(row["market"], row["code"]): row for row in contract["rows"]}
    assert contract["expectedPreviousDate"] == "2026-08-21"
    assert rows[("SSE", "510300")]["changeShares"] == 30
    assert rows[("SSE", "510300")]["aligned"] is True
    assert rows[("SSE", "510500")]["alignmentReason"] == "new_listing"
    assert rows[("SSE", "510500")]["changeShares"] is None
    assert rows[("SSE", "510999")]["alignmentReason"] == "missing_previous"
    assert rows[("SSE", "510999")]["changeShares"] is None
    assert rows[("SSE", "510050")]["alignmentReason"] == "delisted"
    assert rows[("SSE", "510050")]["currentShares"] is None
    assert rows[("SZSE", "159919")]["alignmentReason"] == "delayed_market"
    assert contract["marketStatus"][1]["status"] == "delayed_market"
    assert contract["counts"] == {
        "totalRows": 5,
        "alignedRows": 1,
        "newListingRows": 1,
        "missingPreviousRows": 1,
        "delistedRows": 1,
        "delayedMarketRows": 1,
    }


def test_contract_fingerprint_is_stable_and_sensitive_to_source_lineage() -> None:
    contract = build_official_etf_daily_changes(_frame(), as_of="2026-08-22")
    same = official_etf_daily_changes_fingerprint(
        as_of=contract["asOf"],
        expected_previous_date=contract["expectedPreviousDate"],
        source_release_ids=[],
        rows=list(reversed(contract["rows"])),
    )
    assert same == contract["inputSha256"]
    changed = official_etf_daily_changes_fingerprint(
        as_of=contract["asOf"],
        expected_previous_date=contract["expectedPreviousDate"],
        source_release_ids=["different-release"],
        rows=contract["rows"],
    )
    assert changed != contract["inputSha256"]


def test_reconcile_reports_per_code_and_aggregate_differences() -> None:
    contract = build_official_etf_daily_changes(_frame(), as_of="2026-08-22")
    legacy = [
        {
            "market": "SSE",
            "code": "510300",
            "previousTradeDate": "2026-08-21",
            "currentShares": 130,
            "previousShares": 100,
            "value": 0.0000003,
        }
    ]
    report = reconcile_official_etf_daily_changes(contract, legacy)
    assert report["status"] == "mismatch"
    assert report["differenceCount"] == 4
    assert report["aggregate"]["offlineChangeShares"] == 30
    assert report["aggregate"]["legacyChangeShares"] == 30
    assert any(item["fields"].get("row") == "legacy_missing" for item in report["differences"])


def test_archived_fixture_reconciles_old_and_new_algorithms() -> None:
    canonical = pd.read_csv(
        FIXTURE_ROOT / "canonical.csv", dtype={"instrument_code": str}
    )
    legacy = json.loads((FIXTURE_ROOT / "legacy.json").read_text(encoding="utf-8"))
    contract = build_official_etf_daily_changes(canonical, as_of="2026-08-22")
    report = reconcile_official_etf_daily_changes(contract, legacy["rows"])
    assert report["status"] == "pass"
    assert report["differenceCount"] == 0
    assert report["aggregate"]["offlineChangeShares"] == 80


def test_candidate_limit_is_hard_capped_at_two() -> None:
    args = Namespace(candidate_date=["2026-08-20", "2026-08-21", "2026-08-22"], as_of=None)
    with pytest.raises(ValueError, match="at most two"):
        _candidate_dates(args, _frame())


def test_candidate_dates_must_be_unique() -> None:
    args = Namespace(candidate_date=["2026-08-22", "2026-08-22"], as_of=None)
    with pytest.raises(ValueError, match="unique"):
        _candidate_dates(args, _frame())


def test_canonical_future_dates_and_duplicates_are_rejected() -> None:
    future = _frame().assign(trade_date=lambda frame: frame["trade_date"].where(frame.index != 0, "2026-08-23"))
    with pytest.raises(ValueError, match="future"):
        build_official_etf_daily_changes(future, as_of="2026-08-22")
    duplicate = pd.concat([_frame(), _frame().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        build_official_etf_daily_changes(duplicate, as_of="2026-08-22")


def test_canonical_shares_tolerate_official_sse_float_noise() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-08-21",
                "market": "SSE",
                "instrument_code": "510300",
                "shares": 6_826_266_800.000001,
            },
            {
                "trade_date": "2026-08-22",
                "market": "SSE",
                "instrument_code": "510300",
                "shares": 964_406_799.9999999,
            },
        ]
    )

    contract = build_official_etf_daily_changes(frame, as_of="2026-08-22")

    assert contract["rows"][0]["previousShares"] == 6_826_266_800
    assert contract["rows"][0]["currentShares"] == 964_406_800


def test_canonical_shares_reject_material_fractional_values() -> None:
    frame = _frame().copy()
    frame["shares"] = frame["shares"].astype(float)
    frame.loc[0, "shares"] = 100.25

    with pytest.raises(ValueError, match="canonical_etf_shares_invalid"):
        build_official_etf_daily_changes(frame, as_of="2026-08-22")
