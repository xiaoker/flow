from datetime import date, timedelta

import pytest

from trading_desk.stock_pressure import StockPressureError, build_stock_pressure, merge_stock_catalog_history


def _catalog(as_of="2026-08-06"):
    return {
        "schemaVersion": 1,
        "asOf": as_of,
        "basis": {"source": "official"},
        "coverage": {"matchedEtfCount": 2},
        "items": [
            {
                "listingMarket": "101",
                "listingMarketName": "上交所",
                "stockCode": "600000",
                "stockName": "浦发银行",
                "netShares": 100,
                "inflowShares": 160,
                "outflowShares": 60,
                "etfCount": 2,
                "contributors": [],
            }
        ],
        "warnings": [],
    }


def test_history_merge_is_idempotent_and_bounded():
    current = _catalog()
    previous = _catalog("2026-08-05")
    previous["items"][0]["netShares"] = -40
    previous["items"][0]["inflowShares"] = 20
    previous["items"][0]["outflowShares"] = 60

    previous_items = [dict(item, asOf=previous["asOf"]) for item in previous["items"]]
    merged = merge_stock_catalog_history(current, previous_items)
    again = merge_stock_catalog_history(merged, merged["items"])
    history = merged["items"][0]["history"]
    assert [node["date"] for node in history] == ["2026-08-05", "2026-08-06"]
    assert again["items"][0]["history"] == history

    for index in range(30):
        current["asOf"] = (date(2026, 9, 1) + timedelta(days=index)).isoformat()
        current = merge_stock_catalog_history(current, current["items"])
    assert len(current["items"][0]["history"]) == 21


def test_pressure_keeps_gross_flow_when_net_is_zero():
    catalog = _catalog()
    catalog["items"][0]["netShares"] = 0
    catalog["items"][0]["inflowShares"] = 100
    catalog["items"][0]["outflowShares"] = 100
    catalog = merge_stock_catalog_history(catalog)
    pressure = build_stock_pressure(catalog)
    row = pressure["stocks"][0]
    assert pressure["summary"]["netShares"] == 0
    assert pressure["summary"]["grossShares"] == 200
    assert row["historyCoverage"] == 1
    assert row["rolling20"] is None


def test_pressure_rejects_non_finite_or_inconsistent_flow() -> None:
    catalog = _catalog()
    catalog["items"][0]["inflowShares"] = float("nan")
    with pytest.raises(StockPressureError, match="pressure_field_invalid:inflowShares"):
        merge_stock_catalog_history(catalog)

    catalog = _catalog()
    catalog["items"][0]["netShares"] = 99
    with pytest.raises(StockPressureError, match="pressure_flow_identity_mismatch"):
        merge_stock_catalog_history(catalog)


def test_pressure_does_not_roll_across_missing_global_partition() -> None:
    catalog = _catalog("2026-01-22")
    global_dates = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(21)]
    item_dates = [global_dates[0], *global_dates[2:], "2026-01-22"]
    catalog["availableDates"] = global_dates
    catalog["items"][0]["history"] = [
        {
            "date": item_date,
            "netShares": 100,
            "inflowShares": 160,
            "outflowShares": 60,
            "penetratedShares": 100,
            "etfCount": 2,
        }
        for item_date in item_dates
    ]

    pressure = build_stock_pressure(catalog)

    assert pressure["stocks"][0]["historyCoverage"] == 21
    assert pressure["stocks"][0]["rolling20"] is None
