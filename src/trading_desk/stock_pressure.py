"""Pure ETF-to-stock pressure read-model helpers.

The pressure workbench is a projection of the already validated ETF PCF
calculation.  This module deliberately does not fetch data, infer prices, or
read storage.  The daily build supplies the current stock catalog and, when
available, the previous release's stock-shard rows.  We then carry forward a
bounded history of real published nodes and produce a compact dashboard
payload.
"""

from __future__ import annotations

import math
import hashlib
from copy import deepcopy
from datetime import date
import re
from typing import Any, Iterable


# v2 separates the bounded pressure index from the full per-stock history and
# contributor details stored in stock_* shards.
PRESSURE_SCHEMA_VERSION = 2
HISTORY_NODE_LIMIT = 21
ROLLING_WINDOW = 20
STOCK_SHARD_COUNT = 8


class StockPressureError(ValueError):
    """Raised when a pressure catalog cannot be safely merged or published."""


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not ISO_DATE.fullmatch(text):
        raise StockPressureError(f"pressure_{field}_invalid")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise StockPressureError(f"pressure_{field}_invalid") from exc
    return text


def _record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StockPressureError("pressure_record_expected")
    return value


def stock_identity(item: dict[str, Any]) -> str:
    market = str(item.get("listingMarket", "")).strip()
    code = str(item.get("stockCode", "")).strip()
    if not market or not code:
        raise StockPressureError("pressure_stock_identity_missing")
    return f"{market}:{code}"


def stock_shard_name(listing_market: Any, stock_code: Any) -> str:
    """Return the stable read-model shard for one listing-market/code key.

    The pressure index and the detailed stock artifacts must agree on this
    mapping.  Keeping the hash here avoids duplicating a subtly different
    bucket rule in the offline projection and the web client.
    """

    identity = f"{str(listing_market).strip()}:{str(stock_code).strip()}"
    bucket = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16)
    return f"stock_{bucket % STOCK_SHARD_COUNT}"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: Any, digits: int = 2) -> float | None:
    number = _number(value)
    return None if number is None else round(number, digits)


def _required_number(item: dict[str, Any], field: str, *, minimum: float | None = None) -> float:
    if field not in item:
        raise StockPressureError(f"pressure_field_missing:{field}")
    number = _number(item.get(field))
    if number is None or (minimum is not None and number < minimum):
        raise StockPressureError(f"pressure_field_invalid:{field}")
    return number


def _validate_flow_fields(item: dict[str, Any]) -> None:
    net = _required_number(item, "netShares")
    inflow = _required_number(item, "inflowShares", minimum=0)
    outflow = _required_number(item, "outflowShares", minimum=0)
    if abs(net - (inflow - outflow)) > 0.01:
        raise StockPressureError("pressure_flow_identity_mismatch")
    etf_count = item.get("etfCount")
    try:
        etf_count_value = int(etf_count)
    except (TypeError, ValueError) as exc:
        raise StockPressureError("pressure_etf_count_invalid") from exc
    if etf_count_value < 0 or float(etf_count_value) != float(etf_count):
        raise StockPressureError("pressure_etf_count_invalid")
    if "penetratedShares" in item and item.get("penetratedShares") is not None:
        _required_number(item, "penetratedShares", minimum=0)


def _node(item: dict[str, Any], date: str) -> dict[str, Any]:
    """Return only the time-varying fields needed by the pressure chart."""

    _validate_flow_fields(item)

    return {
        "date": _date(date, field="history_date"),
        "netShares": _rounded(item.get("netShares")),
        "inflowShares": _rounded(item.get("inflowShares")),
        "outflowShares": _rounded(item.get("outflowShares")),
        "penetratedShares": _rounded(item.get("penetratedShares")),
        "etfCount": int(item.get("etfCount")),
    }


def _valid_node(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        node_date = _date(value.get("date"), field="history_date")
    except StockPressureError:
        return None
    try:
        _validate_flow_fields(value)
    except StockPressureError:
        return None
    return {
        "date": node_date,
        "netShares": _rounded(value.get("netShares")),
        "inflowShares": _rounded(value.get("inflowShares")),
        "outflowShares": _rounded(value.get("outflowShares")),
        "penetratedShares": _rounded(value.get("penetratedShares")),
        "etfCount": int(value.get("etfCount")),
    }


def _history_for(item: dict[str, Any], *, fallback_date: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    raw_history = item.get("history")
    if isinstance(raw_history, list):
        values.extend(node for node in (_valid_node(value) for value in raw_history) if node)
    if not values and fallback_date:
        values.append(_node(item, fallback_date))
    by_date = {node["date"]: node for node in values}
    return [by_date[key] for key in sorted(by_date)[-HISTORY_NODE_LIMIT:]]


def merge_stock_catalog_history(
    current_catalog: dict[str, Any],
    previous_items: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Attach a bounded history to the current stock catalog.

    Previous shards may already contain a history list.  Older releases only
    contain the point-in-time fields, so their ``asOf`` snapshot is promoted
    to one historical node.  The current date always replaces a same-date
    node, making a rerun idempotent.  Stocks absent from the current catalog
    are intentionally not carried forward: the table represents the current
    confirmed ETF basket universe.
    """

    catalog = _record(deepcopy(current_catalog))
    if catalog.get("schemaVersion") != 1:
        raise StockPressureError("pressure_catalog_schema_invalid")
    as_of = _date(catalog.get("asOf"), field="catalog_as_of")
    current_items = catalog.get("items")
    if not isinstance(current_items, list):
        raise StockPressureError("pressure_catalog_items_missing")

    previous_by_identity: dict[str, dict[str, Any]] = {}
    for raw in previous_items:
        item = _record(raw)
        identity = stock_identity(item)
        if identity in previous_by_identity:
            raise StockPressureError(f"pressure_duplicate_previous_identity:{identity}")
        previous_by_identity[identity] = item

    seen: set[str] = set()
    for raw in current_items:
        item = _record(raw)
        _validate_flow_fields(item)
        identity = stock_identity(item)
        if identity in seen:
            raise StockPressureError(f"pressure_duplicate_current_identity:{identity}")
        seen.add(identity)
        previous = previous_by_identity.get(identity)
        history = _history_for(previous, fallback_date=str(previous.get("asOf", ""))) if previous else []
        # A previous item without a valid asOf has no trustworthy point.  The
        # current catalog still remains usable, but does not receive a made-up
        # historical node.
        history = [node for node in history if node["date"] < as_of]
        # A rerun may pass through an already enriched catalog.  Keep its
        # older nodes, but always append the current point from the current
        # day's fields so a new date cannot silently disappear.
        current_history = item.get("history")
        if isinstance(current_history, list):
            history.extend(node for node in (_valid_node(value) for value in current_history) if node and node["date"] < as_of)
        history.append(_node(item, as_of))
        by_date = {node["date"]: node for node in history}
        item["history"] = [by_date[key] for key in sorted(by_date)[-HISTORY_NODE_LIMIT:]]
        item["historyCoverage"] = len(item["history"])
    catalog["pressureSchemaVersion"] = PRESSURE_SCHEMA_VERSION
    catalog["historyNodeLimit"] = HISTORY_NODE_LIMIT
    return catalog


def build_stock_pressure(
    catalog: dict[str, Any],
    *,
    market_financing_balance_yuan: float | None = None,
    margin_as_of: str | None = None,
) -> dict[str, Any]:
    """Build the compact, stock-centric ETF β压强 read model."""

    value = _record(catalog)
    if value.get("schemaVersion") != 1:
        raise StockPressureError("pressure_catalog_schema_invalid")
    as_of = str(value.get("asOf", "")).strip()
    items = value.get("items")
    try:
        as_of = _date(as_of, field="catalog_as_of")
    except StockPressureError:
        raise
    if not isinstance(items, list):
        raise StockPressureError("pressure_catalog_invalid")

    rows: list[dict[str, Any]] = []
    all_dates: set[str] = set()
    history20_count = 0
    for raw in items:
        item = _record(raw)
        _validate_flow_fields(item)
        identity = stock_identity(item)
        history = [node for node in _history_for(item, fallback_date=as_of) if node["date"] <= as_of]
        by_date = {node["date"]: node for node in history}
        by_date[as_of] = _node(item, as_of)
        history = [by_date[key] for key in sorted(by_date)[-HISTORY_NODE_LIMIT:]]
        all_dates.update(node["date"] for node in history)
        rolling = None
        if len(history) >= ROLLING_WINDOW:
            window = history[-ROLLING_WINDOW:]
            rolling = {
                "from": window[0]["date"],
                "to": window[-1]["date"],
                "coverage": len(window),
                "netShares": round(sum(node["netShares"] for node in window), 2),
                "inflowShares": round(sum(node["inflowShares"] for node in window), 2),
                "outflowShares": round(sum(node["outflowShares"] for node in window), 2),
            }
            history20_count += 1
        rows.append(
            {
                "identity": identity,
                "listingMarket": str(item.get("listingMarket", "")),
                "listingMarketName": str(item.get("listingMarketName", "未知市场")),
                "stockCode": str(item.get("stockCode", "")),
                "stockName": str(item.get("stockName", "")),
                # Full history and contributor lists live in the immutable
                # stock shard.  The pressure index only needs this pointer
                # to resolve the selected row without inflating the page
                # payload beyond the R2/Worker object limit.
                "shard": stock_shard_name(item.get("listingMarket"), item.get("stockCode")),
                "netShares": _rounded(item.get("netShares")) or 0.0,
                "inflowShares": _rounded(item.get("inflowShares")) or 0.0,
                "outflowShares": _rounded(item.get("outflowShares")) or 0.0,
                "penetratedShares": _rounded(item.get("penetratedShares")),
                "etfCount": int(item.get("etfCount") or 0),
                "historyCoverage": len(history),
                "rolling20": rolling,
                # Optional fields are passed through only when an official
                # source adapter has supplied them.  Missing values stay null.
                "closePriceYuan": _rounded(item.get("closePriceYuan"), 4),
                "floatShares": _rounded(item.get("floatShares")),
                "floatMarketCapYuan": _rounded(item.get("floatMarketCapYuan")),
                "marginBalanceYuan": _rounded(item.get("marginBalanceYuan")),
                "shortBalanceYuan": _rounded(item.get("shortBalanceYuan")),
                "priceAsOf": item.get("priceAsOf"),
                "marginAsOf": item.get("marginAsOf"),
            }
        )

    rows.sort(key=lambda row: (-abs(float(row["netShares"])), row["stockName"], row["stockCode"]))
    summary = {
        "netShares": round(sum(float(row["netShares"]) for row in rows), 2),
        "inflowShares": round(sum(float(row["inflowShares"]) for row in rows), 2),
        "outflowShares": round(sum(float(row["outflowShares"]) for row in rows), 2),
    }
    summary["grossShares"] = round(summary["inflowShares"] + summary["outflowShares"], 2)
    summary["marketFinancingBalanceYuan"] = _rounded(market_financing_balance_yuan)
    return {
        "schemaVersion": PRESSURE_SCHEMA_VERSION,
        "asOf": as_of,
        "basis": {
            "unit": "股",
            "calculation": "ETF份额变化 ÷ 最小申赎单位 × PCF成分数量",
            "identity": "上市市场 + 个股代码",
            "interpretation": "ETF申赎篮子穿透等价，不等同真实现金流或基金实际持仓变动",
            "source": (
                value.get("basis", {}).get("source", "官方 ETF 份额快照 + PCF")
                if isinstance(value.get("basis"), dict)
                else "官方 ETF 份额快照 + PCF"
            ),
            "priceSource": "未接入经 QA 验证的官方收盘价时保持为空",
        },
        "summary": summary,
        "coverage": {
            "stockCount": len(rows),
            "matchedEtfCount": int(value.get("coverage", {}).get("matchedEtfCount", 0)),
            "history20StockCount": history20_count,
            "history20Coverage": round(history20_count / len(rows) * 100, 2) if rows else 0,
            "priceCount": sum(row["closePriceYuan"] is not None for row in rows),
            "marginCount": sum(row["marginBalanceYuan"] is not None for row in rows),
        },
        "marginAsOf": margin_as_of,
        "historyStatus": value.get("historyStatus", "unknown"),
        "historyContinuity": value.get("historyContinuity"),
        "availableDates": sorted(all_dates),
        "stocks": rows,
        "warnings": list(value.get("warnings", [])),
    }
