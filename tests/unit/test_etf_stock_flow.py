import pandas as pd
import pytest

from trading_desk.etf_stock_flow import (
    StockFlowInputError,
    build_stock_flow,
    build_stock_flow_catalog,
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
    assert result["etfs"][0]["code"] == "588000"
    assert result["etfs"][0]["stocks"][0]["stockCode"] == "688008"
    assert result["etfs"][0]["stockShownCount"] == 1
    assert result["coverage"]["matchedEtfCount"] == 1


def test_next_day_pcf_uses_explicit_pre_trading_day_alignment() -> None:
    pcf = _pcf().assign(
        pcf_date="2026-07-03",
        pcf_previous_date="2026-07-02",
        date_basis="sse_pcf_next_day_previous_trading_day",
    )
    result = build_stock_flow(_shares(), pcf, as_of="2026-07-02", previous_date="2026-07-01")

    assert result["available"] is True
    assert result["basis"]["pcfDate"] == "2026-07-03"
    assert result["basis"]["pcfAlignment"] == "next_day_previous_trade_date"
    assert any("PRE_TRADING_DAY" in warning for warning in result["warnings"])


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


def test_same_numeric_code_on_two_listing_markets_is_not_merged() -> None:
    pcf = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-02",
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "000883",
                "stock_name": "湖北能源",
                "listing_market": "102",
                "quantity": 300,
                "creation_unit": 3_000_000,
            },
            {
                "trade_date": "2026-07-02",
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "00883",
                "stock_name": "中海油",
                "listing_market": "103",
                "quantity": 1397,
                "creation_unit": 3_000_000,
            },
        ]
    )
    result = build_stock_flow(_shares(), pcf, as_of="2026-07-02", previous_date="2026-07-01")

    assert result["available"] is True
    identities = {(row["listingMarket"], row["stockCode"]) for row in result["stocks"]}
    assert identities == {("102", "000883"), ("103", "00883")}


def test_cash_substitution_is_visible_as_an_estimate_caveat() -> None:
    result = build_stock_flow(
        _shares(), _pcf(flag="1"), as_of="2026-07-02", previous_date="2026-07-01"
    )

    assert result["available"] is True
    assert any("现金替代" in warning for warning in result["warnings"])
    assert result["stocks"][0]["contributors"][0]["estimated"] is True


def test_unconfirmed_pcf_security_is_not_counted_as_a_stock() -> None:
    pcf = pd.concat(
        [
            _pcf(),
            _pcf().assign(stock_code="CASH", stock_name="现金替代", quantity=1),
        ],
        ignore_index=True,
    )
    result = build_stock_flow(_shares(), pcf, as_of="2026-07-02", previous_date="2026-07-01")

    assert result["available"] is True
    assert [(row["listingMarket"], row["stockCode"]) for row in result["stocks"]] == [("101", "688008")]


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


def test_same_stock_across_etf_markets_uses_one_listing_identity() -> None:
    shares = pd.DataFrame(
        [
            {"trade_date": "2026-07-01", "market": "SSE", "instrument_code": "588000", "shares": 3_000_000},
            {"trade_date": "2026-07-02", "market": "SSE", "instrument_code": "588000", "shares": 6_000_000},
            {"trade_date": "2026-07-01", "market": "SZSE", "instrument_code": "159900", "shares": 3_000_000},
            {"trade_date": "2026-07-02", "market": "SZSE", "instrument_code": "159900", "shares": 6_000_000},
        ]
    )
    pcf = pd.DataFrame(
        [
            {**_pcf().iloc[0].to_dict(), "market": "SSE", "etf_code": "588000", "stock_code": "000883", "listing_market": "102"},
            {**_pcf().iloc[0].to_dict(), "market": "SZSE", "etf_code": "159900", "stock_code": "000883", "listing_market": "102"},
        ]
    )

    result = build_stock_flow(shares, pcf, as_of="2026-07-02", previous_date="2026-07-01")

    assert result["coverage"]["stockCount"] == 1
    assert {(row["listingMarket"], row["stockCode"]) for row in result["stocks"]} == {("102", "000883")}
    assert result["stocks"][0]["etfCount"] == 2


def test_same_etf_code_on_two_markets_is_counted_twice() -> None:
    shares = pd.DataFrame(
        [
            {"trade_date": "2026-07-01", "market": "SSE", "instrument_code": "588000", "shares": 3_000_000},
            {"trade_date": "2026-07-02", "market": "SSE", "instrument_code": "588000", "shares": 6_000_000},
            {"trade_date": "2026-07-01", "market": "SZSE", "instrument_code": "588000", "shares": 3_000_000},
            {"trade_date": "2026-07-02", "market": "SZSE", "instrument_code": "588000", "shares": 6_000_000},
        ]
    )
    pcf = pd.DataFrame(
        [
            {**_pcf().iloc[0].to_dict(), "market": "SSE", "etf_code": "588000", "listing_market": "101"},
            {**_pcf().iloc[0].to_dict(), "market": "SZSE", "etf_code": "588000", "listing_market": "101"},
        ]
    )

    result = build_stock_flow(shares, pcf, as_of="2026-07-02", previous_date="2026-07-01")

    assert result["stocks"][0]["etfCount"] == 2
    assert {(row["market"], row["code"]) for row in result["etfs"]} == {("SSE", "588000"), ("SZSE", "588000")}


def _security_margin(*, date: str = "2026-07-02", code: str = "688008", market: str = "SSE") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": date,
                "market": market,
                "stock_code": code,
                "security_name": "澜起科技",
                "financing_balance_yuan": 1_234_500_000,
                "financing_buy_yuan": 50_000_000,
                "financing_repayment_yuan": 45_000_000,
                "securities_lending_balance_shares": 12_300,
                "securities_lending_sell_shares": 1_200,
                "securities_lending_repayment_shares": 900,
                "source_id": "sse",
            }
        ]
    )


def test_catalog_joins_exact_date_official_margin_without_filling_other_markets() -> None:
    catalog = build_stock_flow_catalog(
        _shares(),
        _pcf(),
        as_of="2026-07-02",
        previous_date="2026-07-01",
        security_margin_frame=_security_margin(),
    )

    row = catalog["items"][0]
    assert row["listingMarket"] == "101"
    assert row["marginBalanceYuan"] == 1_234_500_000.0
    assert row["financingBuyYuan"] == 50_000_000.0
    assert row["shortBalanceShares"] == 12_300.0
    assert row["marginAsOf"] == "2026-07-02"
    coverage = catalog["coverage"]["securityMarginCoverage"]
    assert coverage["available"] is True
    assert coverage["matchedCount"] == 1
    assert coverage["availableMarkets"] == ["SSE"]
    assert coverage["unsupportedMarkets"] == ["SZSE", "BSE"]


def test_catalog_rejects_duplicate_security_margin_business_key() -> None:
    duplicate = pd.concat([_security_margin(), _security_margin()], ignore_index=True)

    with pytest.raises(StockFlowInputError, match="security_margin_duplicate_business_key"):
        build_stock_flow_catalog(
            _shares(),
            _pcf(),
            as_of="2026-07-02",
            previous_date="2026-07-01",
            security_margin_frame=duplicate,
        )
