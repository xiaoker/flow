from __future__ import annotations

import pandas as pd

from trading_desk.pcf_parity import compare_pcf_frames, summarize_pcf_parity_reports


def _row(**overrides: object) -> dict[str, object]:
    row = {
        "etf_code": "510300",
        "etf_name": "沪深300ETF",
        "stock_code": "600000",
        "stock_name": "浦发银行",
        "listing_market": "101",
        "quantity": 100,
        "creation_unit": 1_000_000,
        "substitution_flag": "0",
        "pcf_date": "2026-08-20",
        "pcf_previous_date": "2026-08-19",
        "date_basis": "sse_pcf_next_day_previous_trading_day",
    }
    row.update(overrides)
    return row


def test_compare_classifies_expected_date_shift_without_anomaly() -> None:
    legacy = pd.DataFrame([_row()])
    v2 = pd.DataFrame([_row(pcf_date="2026-08-21", pcf_previous_date="2026-08-20", date_basis="sse_pcf_exact_same_trading_day_shadow")])
    result = compare_pcf_frames(legacy, v2)
    assert result["byCategory"] == {"DATE_SEMANTICS_CHANGE": 1}
    assert result["unexpectedAnomalyCount"] == 0


def test_compare_exposes_zero_quantity_omission_and_real_anomaly() -> None:
    legacy = pd.DataFrame([_row(quantity=90), _row(stock_code="600001", quantity=10)])
    v2 = pd.DataFrame([_row(quantity=100), _row(stock_code="600002", quantity=0)])
    result = compare_pcf_frames(legacy, v2)
    assert result["byCategory"]["REAL_DATA_ANOMALY"] == 2
    assert result["byCategory"]["LEGACY_ZERO_QUANTITY_OMISSION"] == 1
    assert result["unexpectedAnomalyCount"] == 2


def test_five_day_summary_does_not_pass_with_missing_observations() -> None:
    report = {
        "shadowOnly": True,
        "tradeDate": "2026-08-21",
        "status": "PASS",
        "coverage": {"complete": True},
        "comparison": {"unexpectedAnomalyCount": 0},
        "budget": {"http403429Count": 0, "totalRequests": 3, "localBytes": 10, "r2BytesWritten": 0, "d1Writes": 0, "estimatedActionsMinutes": 1},
    }
    summary = summarize_pcf_parity_reports([report], required_days=5)
    assert summary["status"] == "INSUFFICIENT_OBSERVATIONS"
    assert summary["observedDays"] == 1
