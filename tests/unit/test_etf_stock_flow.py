import pandas as pd
import pytest

from trading_desk.etf_stock_flow import (
    StockFlowInputError,
    build_stock_flow,
)


def _shares(current: float = 31_131_000_000, previous: float = 29_964_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": "2026-07-01", "market": "SSE", "instrument_code": "588000", "instrument_name": "科创50ETF", "shares": previous},
            {"trade_date": "2026-07-02", "market": "SSE", "instrument_code": "588000", "instrument_name": "科创50ETF", "shares": current},
        ]
    )


def _pcf(*, date: str = "2026-07-02", flag: str = "") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": date,
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "688008",
                "stock_name": "澜起科技",
                "quantity": 1812,
                "creation_unit": 3_000_000,
                "substitution_flag": flag,
            }
        ]
    )


def test_same_day_pcf_calculates_the_expected_stock_equivalent() -> None:
    result = build_stock_flow(_shares(), _pcf(), as_of="2026-07-02", previous_date="2026-07-01")

    assert result["available"] is True
    assert result["basis"]["shareDate"] == "2026-07-02"
    assert result["basis"]["pcfDate"] == "2026-07-02"
    # 11.67e8 / 3e6 * 1812 = 704,868 shares = 70.4868万股.
    assert result["stocks"][0]["stockCode"] == "688008"
    assert result["stocks"][0]["netShares"] == 704_868.0
    assert result["coverage"]["matchedEtfCount"] == 1


def test_pcf_date_mismatch_is_unavailable_and_never_falls_back() -> None:
    result = build_stock_flow(
        _shares(), _pcf(date="2026-07-01"), as_of="2026-07-02", previous_date="2026-07-01"
    )

    assert result["available"] is False
    assert "pcf_date_mismatch" in result["warnings"]
    assert result["stocks"] == []


def test_duplicate_component_key_is_a_hard_quality_failure() -> None:
    duplicate = pd.concat([_pcf(), _pcf()], ignore_index=True)

    with pytest.raises(StockFlowInputError, match="duplicate_pcf_component_key"):
        build_stock_flow(_shares(), duplicate, as_of="2026-07-02", previous_date="2026-07-01")


def test_cash_substitution_is_visible_as_an_estimate_caveat() -> None:
    result = build_stock_flow(
        _shares(), _pcf(flag="1"), as_of="2026-07-02", previous_date="2026-07-01"
    )

    assert result["available"] is True
    assert any("现金替代" in warning for warning in result["warnings"])
    assert result["stocks"][0]["contributors"][0]["estimated"] is True


def test_positive_and_negative_etf_flows_aggregate_on_the_same_stock() -> None:
    shares = pd.DataFrame(
        [
            {"trade_date": "2026-07-01", "market": "SSE", "instrument_code": "588000", "shares": 3_000_000},
            {"trade_date": "2026-07-02", "market": "SSE", "instrument_code": "588000", "shares": 6_000_000},
            {"trade_date": "2026-07-01", "market": "SSE", "instrument_code": "588001", "shares": 6_000_000},
            {"trade_date": "2026-07-02", "market": "SSE", "instrument_code": "588001", "shares": 3_000_000},
        ]
    )
    pcf = pd.concat(
        [
            _pcf().assign(etf_code="588000", quantity=100),
            _pcf().assign(etf_code="588001", quantity=100),
        ],
        ignore_index=True,
    )

    result = build_stock_flow(shares, pcf, as_of="2026-07-02", previous_date="2026-07-01")

    stock = result["stocks"][0]
    assert stock["netShares"] == 0
    assert stock["inflowShares"] == 100
    assert stock["outflowShares"] == 100
    assert stock["etfCount"] == 2
