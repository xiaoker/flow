from __future__ import annotations

import pandas as pd

from trading_desk.phase0 import _coverage_finding, _required_coverage_dates
from trading_desk.quality import (
    summarize_findings,
    validate_connect_summary,
    validate_etf,
    validate_etf_nav,
    validate_bse_margin,
    validate_margin,
)


def test_szse_coverage_requires_current_target_but_not_snapshot_history() -> None:
    requested = ["2026-08-05", "2026-08-06", "2026-08-07"]

    assert _required_coverage_dates("szse_etf", requested) == ["2026-08-07"]
    ready = _coverage_finding(
        "szse_etf",
        ["2026-08-07"],
        _required_coverage_dates("szse_etf", requested),
        remediation="source not ready",
    )
    stale = _coverage_finding(
        "szse_etf",
        ["2026-08-06"],
        _required_coverage_dates("szse_etf", requested),
        remediation="source not ready",
    )

    assert ready.status == "pass"
    assert stale.status == "fail"
    assert stale.evidence["missing_dates"] == ["2026-08-07"]


def test_margin_total_consistency_passes_with_szse_rounding() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-14",
                "market": "SZSE",
                "financing_balance_yuan": 1_417_264_000_000,
                "financing_buy_yuan": 117_491_000_000,
                "securities_lending_balance_yuan": 7_436_000_000,
                "securities_lending_volume_shares": 880_000_000,
                "securities_lending_sell_volume_shares": 27_000_000,
                "margin_total_balance_yuan": 1_424_700_000_000,
            }
        ]
    )

    summary = summarize_findings(validate_margin(frame, "szse_margin"))

    assert summary["counts"]["fail"] == 0
    consistency = next(
        item for item in summary["findings"] if item["check_id"] == "consistency.margin_total"
    )
    assert consistency["evidence"]["max_abs_delta_yuan"] == 0


def test_bse_margin_requires_market_and_source_date_alignment() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-30",
                "source_trade_date": "2026-07-30",
                "market": "BSE",
                "financing_balance_yuan": 8_210_630_112,
                "financing_buy_yuan": 433_272_098,
                "securities_lending_balance_yuan": 454_583,
                "securities_lending_volume_shares": 61_368,
                "securities_lending_sell_volume_shares": 24_200,
                "margin_total_balance_yuan": 8_211_084_695,
            }
        ]
    )

    summary = summarize_findings(validate_bse_margin(frame))

    assert summary["counts"]["fail"] == 0
    assert any(item["check_id"] == "validity.bse_margin_market" for item in summary["findings"])
    assert any(item["check_id"] == "timeliness.bse_source_date" for item in summary["findings"])


def test_empty_bse_margin_is_pending_until_source_publishes() -> None:
    frame = pd.DataFrame(
        columns=[
            "trade_date",
            "market",
            "financing_balance_yuan",
            "financing_buy_yuan",
            "securities_lending_balance_yuan",
            "securities_lending_volume_shares",
            "securities_lending_sell_volume_shares",
            "margin_total_balance_yuan",
            "source_id",
            "source_grade",
            "date_basis",
            "source_trade_date",
            "observed_at",
        ]
    )

    summary = summarize_findings(validate_bse_margin(frame))

    assert summary["counts"]["fail"] == 0
    assert summary["counts"]["warn"] >= 1
    empty = next(
        item for item in summary["findings"] if item["check_id"] == "completeness.non_empty"
    )
    assert empty["status"] == "warn"


def test_connect_rejects_northbound_buy_sell_after_disclosure_change() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "market": "SSE",
                "direction": "northbound",
                "currency": "RMB",
                "total_turnover": 100.0,
                "buy_turnover": 60.0,
                "sell_turnover": 40.0,
                "net_buy": 20.0,
                "total_trade_count": 1,
                "etf_turnover": 2.0,
            }
        ]
    )

    summary = summarize_findings(validate_connect_summary(frame))

    policy = next(
        item for item in summary["findings"] if item["check_id"] == "policy.northbound_no_net_flow"
    )
    assert policy["status"] == "fail"


def test_inferred_szse_etf_date_is_warning_not_silent_pass() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "market": "SZSE",
                "instrument_code": "159001",
                "shares": 1_000_000,
                "nav": 1.0,
                "date_basis": "collector_inferred_current_snapshot",
            }
        ]
    )

    summary = summarize_findings(validate_etf(frame, "szse_etf"))

    source_date = next(
        item for item in summary["findings"] if item["check_id"] == "timeliness.source_date"
    )
    assert source_date["status"] == "warn"
    assert source_date["severity"] == "high"


def test_etf_nav_requires_exact_date_pair_and_nonnegative_value() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "nav_date": "2026-07-14",
                "market": "SSE",
                "instrument_code": "510300",
                "nav": 4.321,
                "date_basis": "source_publication_previous_trading_day_nav",
            }
        ]
    )

    summary = summarize_findings(validate_etf_nav(frame))

    assert summary["counts"]["fail"] == 0
    assert any(
        item["check_id"] == "validity.etf_nav_dates" and item["status"] == "pass"
        for item in summary["findings"]
    )


def test_empty_etf_nav_is_warn_only_optional_enrichment() -> None:
    summary = summarize_findings(validate_etf_nav(pd.DataFrame()))

    completeness = next(
        item for item in summary["findings"] if item["check_id"] == "completeness.non_empty"
    )
    assert completeness["status"] == "warn"
    assert completeness["evidence"]["optional_enrichment"] is True
    assert summary["counts"]["fail"] == 0
