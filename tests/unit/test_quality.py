from __future__ import annotations

import pandas as pd

from trading_desk.quality import (
    summarize_findings,
    validate_connect_summary,
    validate_etf,
    validate_margin,
)


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

