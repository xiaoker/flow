from __future__ import annotations

import pandas as pd
import pytest

from trading_desk.etf_facts import build_canonical_etf_daily_fact, latest_etf_flows_from_terminal


def test_canonical_fact_uses_market_code_date_business_key() -> None:
    dates = ["2026-08-13", "2026-08-14"]
    sse = pd.DataFrame(
        [
            {"trade_date": dates[0], "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 100},
            {"trade_date": dates[1], "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 110},
        ]
    )
    szse = pd.DataFrame(
        [
            {"trade_date": dates[0], "instrument_code": "159919", "instrument_name": "沪深300ETF", "shares": 200},
            {"trade_date": dates[1], "instrument_code": "159919", "instrument_name": "沪深300ETF", "shares": 180},
        ]
    )

    fact = build_canonical_etf_daily_fact(sse, szse, dates)

    assert fact[["trade_date", "market", "instrument_code"]].to_dict("records") == [
        {"trade_date": dates[0], "market": "SSE", "instrument_code": "510300"},
        {"trade_date": dates[0], "market": "SZSE", "instrument_code": "159919"},
        {"trade_date": dates[1], "market": "SSE", "instrument_code": "510300"},
        {"trade_date": dates[1], "market": "SZSE", "instrument_code": "159919"},
    ]


def test_canonical_fact_blocks_duplicate_business_key() -> None:
    rows = pd.DataFrame(
        [
            {"trade_date": "2026-08-14", "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 100},
            {"trade_date": "2026-08-14", "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 101},
        ]
    )

    with pytest.raises(ValueError, match="canonical_etf_duplicate_business_key"):
        build_canonical_etf_daily_fact(rows, pd.DataFrame(), ["2026-08-14"])


def test_homepage_flows_are_derived_from_terminal_rows() -> None:
    flows, summary = latest_etf_flows_from_terminal(
        {
            "instruments": [
                {"market": "SSE", "code": "510300", "name": "沪深300ETF", "periods": {"1d": -2.0}},
                {"market": "SZSE", "code": "159919", "name": "沪深300ETF", "periods": {"1d": 3.0}},
                {"market": "SSE", "code": "510050", "name": "上证50ETF", "periods": {"1d": None}},
            ]
        }
    )

    assert summary == {"net": 1.0, "increasers": 1, "decreasers": 1, "latest_count": 2}
    assert flows["inflow"][0]["code"] == "159919"
    assert flows["outflow"][0]["code"] == "510300"
