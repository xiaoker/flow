from __future__ import annotations

import pandas as pd
import pytest

from trading_desk.stock_flow_history import (
    StockFlowHistoryError,
    build_daily_partitions,
    merge_catalog_with_history,
)


def _shares() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": "2026-08-03", "market": "SSE", "instrument_code": "588000", "shares": 1_000_000},
            {"trade_date": "2026-08-04", "market": "SSE", "instrument_code": "588000", "shares": 2_000_000},
            {"trade_date": "2026-08-05", "market": "SSE", "instrument_code": "588000", "shares": 4_000_000},
        ]
    )


def _pcf() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": date,
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "688008",
                "stock_name": "澜起科技",
                "quantity": 100,
                "creation_unit": 1_000_000,
            }
            for date in ("2026-08-04", "2026-08-05")
        ]
    )


def _current_catalog() -> dict:
    return {
        "schemaVersion": 1,
        "asOf": "2026-08-05",
        "basis": {
            "shareDate": "2026-08-05",
            "previousShareDate": "2026-08-04",
            "pcfDate": "2026-08-05",
        },
        "coverage": {"stockCount": 1, "matchedEtfCount": 1},
        "items": [
            {
                "listingMarket": "101",
                "listingMarketName": "上交所",
                "stockCode": "688008",
                "stockName": "澜起科技",
                "netShares": 200.0,
                "inflowShares": 200.0,
                "outflowShares": 0.0,
                "penetratedShares": 400.0,
                "etfCount": 1,
                "contributors": [],
            }
        ],
    }


def test_build_daily_partitions_keeps_missing_pcf_explicit() -> None:
    result = build_daily_partitions(
        _shares(),
        _pcf(),
        ["2026-08-03", "2026-08-04", "2026-08-05"],
        as_of="2026-08-05",
    )

    assert result["availableDates"] == ["2026-08-04", "2026-08-05"]
    assert result["missingDates"] == [{"asOf": "2026-08-03", "reason": "previous_share_date_missing"}]
    assert [partition["asOf"] for partition in result["partitions"]] == ["2026-08-04", "2026-08-05"]
    assert result["schemaVersion"] == 2
    assert result["partitions"][0]["tradeDate"] == "2026-08-04"
    assert result["partitions"][0]["calculationVersion"] == "etf-stock-pressure-v2"


def test_build_daily_partitions_uses_anchor_for_full_bounded_window() -> None:
    dates = [item.strftime("%Y-%m-%d") for item in pd.date_range("2026-07-01", periods=22, freq="D")]
    shares = pd.DataFrame(
        [
            {"trade_date": trade_date, "market": "SSE", "instrument_code": "588000", "shares": 1_000_000 + index}
            for index, trade_date in enumerate(dates)
        ]
    )
    pcf = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "688008",
                "stock_name": "澜起科技",
                "quantity": 100,
                "creation_unit": 1_000_000,
            }
            for trade_date in dates[1:]
        ]
    )

    result = build_daily_partitions(shares, pcf, dates, as_of=dates[-1])

    assert len(result["requestedDates"]) == 22
    assert len(result["partitions"]) == 21
    assert result["availableDates"][0] == dates[1]
    assert result["availableDates"][-1] == dates[-1]
    assert result["missingDates"] == [{"asOf": dates[0], "reason": "previous_share_date_missing"}]


def test_merge_catalog_with_history_uses_real_partition_nodes() -> None:
    history = build_daily_partitions(
        _shares(),
        _pcf(),
        ["2026-08-03", "2026-08-04", "2026-08-05"],
        as_of="2026-08-05",
    )
    enriched = merge_catalog_with_history(_current_catalog(), history)

    item = enriched["items"][0]
    assert [node["date"] for node in item["history"]] == ["2026-08-04", "2026-08-05"]
    assert item["historyCoverage"] == 2
    assert enriched["historyStatus"] == "partitioned"
    assert enriched["historyContinuity"]["partitionCount"] == 2


def test_merge_catalog_rejects_future_partition() -> None:
    history = build_daily_partitions(_shares(), _pcf(), ["2026-08-04", "2026-08-05"], as_of="2026-08-05")
    history["partitions"].append({**history["partitions"][-1], "asOf": "2026-08-06"})

    with pytest.raises(StockFlowHistoryError, match="history_partition_future_date"):
        merge_catalog_with_history(_current_catalog(), history)


def test_merge_catalog_rejects_current_partition_value_mismatch() -> None:
    history = build_daily_partitions(
        _shares(),
        _pcf(),
        ["2026-08-03", "2026-08-04", "2026-08-05"],
        as_of="2026-08-05",
    )
    history["partitions"][-1]["items"][0]["netShares"] = 999.0
    history["partitions"][-1]["items"][0]["inflowShares"] = 999.0

    with pytest.raises(
        StockFlowHistoryError,
        match=(
            r"current_partition_value_mismatch:101:688008:netShares:"
            r"catalog=200\.0:partition=999\.0"
        ),
    ):
        merge_catalog_with_history(_current_catalog(), history)


def test_merge_catalog_rejects_a_different_previous_share_anchor() -> None:
    history = build_daily_partitions(
        _shares(),
        _pcf(),
        ["2026-08-03", "2026-08-04", "2026-08-05"],
        as_of="2026-08-05",
    )
    history["partitions"][-1]["sharePreviousDate"] = "2026-08-03"
    history["partitions"][-1]["previousDate"] = "2026-08-03"
    history["partitions"][-1]["basis"]["previousShareDate"] = "2026-08-03"

    with pytest.raises(
        StockFlowHistoryError,
        match=(
            r"current_partition_previous_date_mismatch:"
            r"catalog=2026-08-04:partition=2026-08-03"
        ),
    ):
        merge_catalog_with_history(_current_catalog(), history)


def test_august_31_partition_uses_august_28_etf_anchor_not_global_history_gap() -> None:
    shares = pd.DataFrame(
        [
            {"trade_date": "2026-08-27", "market": "SSE", "instrument_code": "510300", "shares": 23_578_687_700},
            {"trade_date": "2026-08-28", "market": "SSE", "instrument_code": "510300", "shares": 23_295_187_700},
            {"trade_date": "2026-08-31", "market": "SSE", "instrument_code": "510300", "shares": 23_503_987_700},
            {"trade_date": "2026-08-27", "market": "SSE", "instrument_code": "510210", "shares": 15_209_192_600},
            {"trade_date": "2026-08-28", "market": "SSE", "instrument_code": "510210", "shares": 14_912_192_600},
            {"trade_date": "2026-08-31", "market": "SSE", "instrument_code": "510210", "shares": 14_621_192_600},
        ]
    )
    pcf = pd.DataFrame(
        [
            {
                "trade_date": "2026-08-31",
                "market": "SSE",
                "etf_code": "510300",
                "etf_name": "300ETF",
                "stock_code": "600000",
                "stock_name": "浦发银行",
                "quantity": 2_100,
                "creation_unit": 900_000,
                "listing_market": "101",
            },
            {
                "trade_date": "2026-08-31",
                "market": "SSE",
                "etf_code": "510210",
                "etf_name": "综指ETF",
                "stock_code": "600000",
                "stock_name": "浦发银行",
                "quantity": 1_200,
                "creation_unit": 3_000_000,
                "listing_market": "101",
            },
        ]
    )

    correct = build_daily_partitions(
        shares,
        pcf,
        ["2026-08-27", "2026-08-28", "2026-08-31"],
        as_of="2026-08-31",
    )
    current = correct["partitions"][-1]
    item = current["items"][0]

    assert current["sharePreviousDate"] == "2026-08-28"
    assert item["stockCode"] == "600000"
    assert item["netShares"] == 370_800.0

    wrong = build_daily_partitions(
        shares,
        pcf,
        ["2026-08-27", "2026-08-31"],
        as_of="2026-08-31",
    )
    assert wrong["partitions"][-1]["sharePreviousDate"] == "2026-08-27"
    assert wrong["partitions"][-1]["items"][0]["netShares"] == -409_500.0
