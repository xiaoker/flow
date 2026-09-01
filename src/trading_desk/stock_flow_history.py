"""Build and validate daily ETF-to-stock flow partitions.

The ETF pressure page is a projection of official ETF share snapshots and
PCF baskets.  A previous pressure release is not a historical source: it is
only a serving projection and can be missing dates after a failed publication.
This module keeps the historical calculation offline and makes each valid
date an explicit, independently verifiable partition.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Iterable

import pandas as pd

from .etf_stock_flow import StockFlowInputError, build_stock_flow_catalog


STOCK_FLOW_HISTORY_SCHEMA_VERSION = 2
HISTORY_NODE_LIMIT = 21
ISO_DATE_LENGTH = 10
CALCULATION_VERSION = "etf-stock-pressure-v2"


class StockFlowHistoryError(ValueError):
    """Raised when daily stock-flow partitions are incomplete or ambiguous."""


def _date(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if len(text) != ISO_DATE_LENGTH:
        raise StockFlowHistoryError(f"{field}_invalid")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise StockFlowHistoryError(f"{field}_invalid") from exc
    return text


def _identity(item: dict[str, Any]) -> str:
    market = str(item.get("listingMarket", "")).strip()
    code = str(item.get("stockCode", "")).strip()
    if not market or not code:
        raise StockFlowHistoryError("stock_identity_missing")
    return f"{market}:{code}"


def _node(item: dict[str, Any], trade_date: str) -> dict[str, Any]:
    return {
        "date": trade_date,
        "netShares": round(float(item.get("netShares", 0) or 0), 2),
        "inflowShares": round(float(item.get("inflowShares", 0) or 0), 2),
        "outflowShares": round(float(item.get("outflowShares", 0) or 0), 2),
        "penetratedShares": (
            None
            if item.get("penetratedShares") is None
            else round(float(item.get("penetratedShares")), 2)
        ),
        "etfCount": int(item.get("etfCount", 0) or 0),
    }


def _partition_from_catalog(
    catalog: dict[str, Any],
    *,
    trade_date: str,
    previous_date: str | None,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = dict(catalog.get("coverage") or {})
    items = catalog.get("items")
    if not isinstance(items, list):
        raise StockFlowHistoryError("partition_items_missing")
    # Revalidate identities and flow accounting before a partition can be
    # written.  The pressure builder performs the same identity check, but a
    # partition is an independently archived evidence object.
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise StockFlowHistoryError("partition_item_invalid")
        identity = _identity(item)
        if identity in seen:
            raise StockFlowHistoryError(f"partition_duplicate_identity:{identity}")
        seen.add(identity)
        net = float(item.get("netShares", 0) or 0)
        inflow = float(item.get("inflowShares", 0) or 0)
        outflow = float(item.get("outflowShares", 0) or 0)
        if inflow < 0 or outflow < 0 or abs(net - (inflow - outflow)) > 0.01:
            raise StockFlowHistoryError(f"partition_flow_identity:{identity}")
    compact_items = [
        {
            "listingMarket": item["listingMarket"],
            "listingMarketName": item.get("listingMarketName", "未知市场"),
            "stockCode": item["stockCode"],
            "stockName": item.get("stockName", item["stockCode"]),
            "netShares": item.get("netShares", 0),
            "inflowShares": item.get("inflowShares", 0),
            "outflowShares": item.get("outflowShares", 0),
            "penetratedShares": item.get("penetratedShares"),
            "etfCount": item.get("etfCount", 0),
        }
        for item in items
    ]
    return {
        "schemaVersion": STOCK_FLOW_HISTORY_SCHEMA_VERSION,
        "partitionType": "etf_stock_flow_daily",
        "available": bool(items),
        "asOf": trade_date,
        "tradeDate": trade_date,
        "sharePreviousDate": previous_date,
        "previousDate": previous_date,
        "pcfDate": (catalog.get("basis") or {}).get("pcfDate"),
        "pcfPublishedDate": (catalog.get("basis") or {}).get("pcfDate"),
        "pcfEffectiveDate": trade_date,
        "calculationVersion": CALCULATION_VERSION,
        "basis": deepcopy(catalog.get("basis") or {}),
        "coverage": coverage,
        "lineage": deepcopy(lineage or {}),
        "artifacts": deepcopy((lineage or {}).get("artifacts") or {}),
        "items": compact_items,
        "warnings": list(catalog.get("warnings") or []),
    }


def build_daily_partitions(
    share_frame: pd.DataFrame,
    pcf_frame: pd.DataFrame,
    trading_dates: Iterable[str],
    *,
    as_of: str | None = None,
    max_partitions: int = HISTORY_NODE_LIMIT,
    lineage_by_date: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build at most ``max_partitions`` real, PCF-aligned daily partitions.

    Dates are selected from the supplied official trading calendar, not from
    the PCF file.  A date whose PCF is missing is retained in ``missingDates``
    with an explicit reason and is never represented as a zero-flow node.
    """

    if max_partitions <= 0:
        raise StockFlowHistoryError("max_partitions_invalid")
    normalized_dates = sorted({_date(value, field="trade_date") for value in trading_dates})
    if as_of is not None:
        target = _date(as_of, field="as_of")
        normalized_dates = [value for value in normalized_dates if value <= target]
    if not normalized_dates:
        raise StockFlowHistoryError("trading_dates_empty")
    # Keep one additional trading date as the previous-share anchor.  The
    # anchor itself is never published as a history node, so the output still
    # stays within ``max_partitions`` while a 21-node window can be built from
    # 22 official dates.
    selected = normalized_dates[-(max_partitions + 1) :]
    partitions: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for index, trade_date in enumerate(selected):
        previous_date = selected[index - 1] if index else None
        if previous_date is None:
            missing.append({"asOf": trade_date, "reason": "previous_share_date_missing"})
            continue
        try:
            catalog = build_stock_flow_catalog(
                share_frame,
                pcf_frame,
                as_of=trade_date,
                previous_date=previous_date,
            )
            coverage = catalog.get("coverage") or {}
            if int(coverage.get("matchedEtfCount", 0) or 0) <= 0 or not catalog.get("items"):
                missing.append({"asOf": trade_date, "reason": "pcf_or_share_pair_missing"})
                continue
            partitions.append(
                _partition_from_catalog(
                    catalog,
                    trade_date=trade_date,
                    previous_date=previous_date,
                    lineage=(lineage_by_date or {}).get(trade_date),
                )
            )
        except (StockFlowInputError, StockFlowHistoryError) as exc:
            missing.append({"asOf": trade_date, "reason": str(exc)})

    available_dates = [partition["asOf"] for partition in partitions]
    return {
        "schemaVersion": STOCK_FLOW_HISTORY_SCHEMA_VERSION,
        "partitionType": "etf_stock_flow_daily_index",
        "asOf": selected[-1],
        "requestedDates": selected,
        "availableDates": available_dates,
        "missingDates": missing,
        "historyNodeLimit": max_partitions,
        "calculationVersion": CALCULATION_VERSION,
        "historyStatus": "partitioned" if partitions else "unavailable",
        "partitions": partitions,
    }


def validate_history_index(
    value: dict[str, Any],
    *,
    as_of: str | None = None,
    max_partitions: int = HISTORY_NODE_LIMIT,
) -> dict[str, Any]:
    """Validate an index before it is used to enrich a current catalog."""

    if value.get("schemaVersion") != STOCK_FLOW_HISTORY_SCHEMA_VERSION:
        raise StockFlowHistoryError("history_schema_invalid")
    if value.get("partitionType") != "etf_stock_flow_daily_index":
        raise StockFlowHistoryError("history_partition_type_invalid")
    target = _date(as_of, field="as_of") if as_of is not None else _date(value.get("asOf"), field="as_of")
    if int(value.get("historyNodeLimit", max_partitions) or 0) > max_partitions:
        raise StockFlowHistoryError("history_node_limit_invalid")
    partitions = value.get("partitions")
    if not isinstance(partitions, list) or len(partitions) > max_partitions:
        raise StockFlowHistoryError("history_partitions_invalid")
    seen_dates: set[str] = set()
    valid: list[dict[str, Any]] = []
    for partition in partitions:
        if not isinstance(partition, dict):
            raise StockFlowHistoryError("history_partition_invalid")
        partition_date = _date(partition.get("asOf"), field="partition_as_of")
        if partition_date > target:
            raise StockFlowHistoryError("history_partition_future_date")
        if partition_date in seen_dates:
            raise StockFlowHistoryError(f"history_duplicate_partition:{partition_date}")
        seen_dates.add(partition_date)
        if partition.get("schemaVersion") != STOCK_FLOW_HISTORY_SCHEMA_VERSION:
            raise StockFlowHistoryError("partition_schema_invalid")
        if partition.get("partitionType") != "etf_stock_flow_daily":
            raise StockFlowHistoryError("partition_type_invalid")
        if partition.get("tradeDate") != partition_date:
            raise StockFlowHistoryError(f"partition_trade_date_mismatch:{partition_date}")
        partition_previous_date = _date(
            partition.get("sharePreviousDate"),
            field="partition_previous_share_date",
        )
        if partition.get("previousDate") != partition_previous_date:
            raise StockFlowHistoryError(
                f"partition_previous_date_mismatch:{partition_date}"
            )
        partition_basis = partition.get("basis")
        if not isinstance(partition_basis, dict):
            raise StockFlowHistoryError("partition_basis_invalid")
        basis_previous_date = _date(
            partition_basis.get("previousShareDate"),
            field="partition_basis_previous_share_date",
        )
        if basis_previous_date != partition_previous_date:
            raise StockFlowHistoryError(
                f"partition_basis_previous_date_mismatch:{partition_date}"
            )
        if partition.get("calculationVersion") != CALCULATION_VERSION:
            raise StockFlowHistoryError("partition_calculation_version_invalid")
        if partition.get("available") is not True:
            raise StockFlowHistoryError(f"partition_not_available:{partition_date}")
        items = partition.get("items")
        if not isinstance(items, list):
            raise StockFlowHistoryError("partition_items_missing")
        identities = {_identity(item) for item in items if isinstance(item, dict)}
        if len(identities) != len(items):
            raise StockFlowHistoryError(f"partition_identity_invalid:{partition_date}")
        for item in items:
            net = float(item.get("netShares", 0) or 0)
            inflow = float(item.get("inflowShares", 0) or 0)
            outflow = float(item.get("outflowShares", 0) or 0)
            if inflow < 0 or outflow < 0 or abs(net - (inflow - outflow)) > 0.01:
                raise StockFlowHistoryError(f"partition_flow_identity:{partition_date}")
        valid.append(partition)
    valid.sort(key=lambda item: item["asOf"])
    value = deepcopy(value)
    value["asOf"] = target
    value["partitions"] = valid
    available_dates = [item["asOf"] for item in valid]
    declared_dates = value.get("availableDates")
    if declared_dates is not None:
        if not isinstance(declared_dates, list) or sorted(str(item) for item in declared_dates) != available_dates:
            raise StockFlowHistoryError("history_available_dates_mismatch")
    value["availableDates"] = available_dates
    return value


def merge_catalog_with_history(
    current_catalog: dict[str, Any],
    history_index: dict[str, Any],
    *,
    max_partitions: int = HISTORY_NODE_LIMIT,
) -> dict[str, Any]:
    """Attach real partition nodes to the current stock catalog.

    The current catalog remains the source of truth for names, contributors
    and current values.  Historical partitions contribute only the bounded
    time-varying fields required by the pressure chart and rolling20 metric.
    """

    if current_catalog.get("schemaVersion") != 1:
        raise StockFlowHistoryError("current_catalog_schema_invalid")
    current_as_of = _date(current_catalog.get("asOf"), field="catalog_as_of")
    history = validate_history_index(history_index, as_of=current_as_of, max_partitions=max_partitions)
    if current_as_of not in history["availableDates"]:
        raise StockFlowHistoryError("current_partition_missing")
    by_identity: dict[str, list[dict[str, Any]]] = {}
    for partition in history["partitions"]:
        partition_date = partition["asOf"]
        for item in partition["items"]:
            identity = _identity(item)
            by_identity.setdefault(identity, []).append(_node(item, partition_date))

    enriched = deepcopy(current_catalog)
    current_items = enriched.get("items")
    if not isinstance(current_items, list):
        raise StockFlowHistoryError("current_catalog_items_missing")
    current_partition = next(
        (partition for partition in history["partitions"] if partition["asOf"] == current_as_of),
        None,
    )
    if current_partition is None:
        # The membership check above is intentionally repeated after sorting
        # and validation so a malformed index cannot make the current node
        # appear authoritative merely because the catalog has the same date.
        raise StockFlowHistoryError("current_partition_missing")
    current_basis = current_catalog.get("basis")
    if not isinstance(current_basis, dict):
        raise StockFlowHistoryError("current_catalog_basis_invalid")
    catalog_previous_date = _date(
        current_basis.get("previousShareDate"),
        field="catalog_previous_share_date",
    )
    partition_previous_date = _date(
        current_partition.get("sharePreviousDate"),
        field="current_partition_previous_share_date",
    )
    if catalog_previous_date != partition_previous_date:
        raise StockFlowHistoryError(
            "current_partition_previous_date_mismatch:"
            f"catalog={catalog_previous_date}:partition={partition_previous_date}"
        )
    current_partition_items = {
        _identity(item): item for item in current_partition["items"] if isinstance(item, dict)
    }
    for item in current_items:
        if not isinstance(item, dict):
            raise StockFlowHistoryError("current_catalog_item_invalid")
        identity = _identity(item)
        partition_item = current_partition_items.get(identity)
        if partition_item is None:
            raise StockFlowHistoryError(f"current_partition_identity_missing:{identity}")
        for field in ("netShares", "inflowShares", "outflowShares", "penetratedShares"):
            current_value = item.get(field)
            partition_value = partition_item.get(field)
            if current_value is None or partition_value is None:
                if current_value is not None or partition_value is not None:
                    raise StockFlowHistoryError(
                        f"current_partition_value_mismatch:{identity}:{field}:"
                        f"catalog={current_value}:partition={partition_value}"
                    )
            elif abs(float(current_value) - float(partition_value)) > 0.01:
                raise StockFlowHistoryError(
                    f"current_partition_value_mismatch:{identity}:{field}:"
                    f"catalog={current_value}:partition={partition_value}"
                )
        if int(item.get("etfCount", 0) or 0) != int(partition_item.get("etfCount", 0) or 0):
            raise StockFlowHistoryError(
                f"current_partition_value_mismatch:{identity}:etfCount:"
                f"catalog={item.get('etfCount')}:partition={partition_item.get('etfCount')}"
            )
        nodes = {node["date"]: node for node in by_identity.get(identity, [])}
        # Always derive the current node from the current catalog.  A stale or
        # missing current partition cannot overwrite the authoritative current
        # snapshot with an older point.
        nodes[current_as_of] = _node(item, current_as_of)
        item["history"] = [nodes[key] for key in sorted(nodes)[-max_partitions:]]
        item["historyCoverage"] = len(item["history"])
    enriched["historyStatus"] = "partitioned"
    enriched["historyNodeLimit"] = max_partitions
    enriched["historyCalculationVersion"] = history.get("calculationVersion", CALCULATION_VERSION)
    enriched["availableDates"] = history["availableDates"]
    enriched["historyContinuity"] = {
        "availableDates": history["availableDates"],
        "requestedDates": history.get("requestedDates", []),
        "missingDates": history.get("missingDates", []),
        "stockCount": len(current_items),
        "partitionCount": len(history["partitions"]),
    }
    warnings = list(enriched.get("warnings") or [])
    if history.get("missingDates"):
        warnings.append(f"stock_flow_history_missing_dates:{len(history['missingDates'])}")
    enriched["warnings"] = list(dict.fromkeys(warnings))
    return enriched
