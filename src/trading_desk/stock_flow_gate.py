"""Resolve the optional ETF-to-stock history state for a release bundle.

The collector always writes ``stock-flow-catalog.json`` when it asks the
dashboard builder for a catalog.  When PCF is unavailable that file is a
small, explicit ``items: []`` sidecar; its presence alone must not be treated
as a partially-built penetration model.  This module is the single contract
used by collection and publication to distinguish that state from a real,
incomplete or ready history bundle.

The functions are deliberately pure.  They only consume already materialized
JSON values and never call D1, R2, a network endpoint, or a workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


VALID_HISTORY_STATUSES = frozenset(
    {"READY", "FAILED_RETRYABLE", "BLOCKED_QUALITY", "NOT_REQUIRED"}
)


class StockFlowGateError(ValueError):
    """Raised when a stock-flow artifact violates the release contract."""


def read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise StockFlowGateError(f"{label}_missing_or_empty")
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StockFlowGateError(f"{label}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise StockFlowGateError(f"{label}_must_be_object")
    return payload


def _declared_stock_flow(snapshot: dict[str, Any]) -> tuple[bool, bool]:
    value = snapshot.get("etfStockFlow")
    if value is None:
        return False, False
    if not isinstance(value, dict):
        raise StockFlowGateError("snapshot_etf_stock_flow_invalid")
    available = value.get("available") is True
    coverage = value.get("coverage")
    if coverage is None:
        stock_count = 0
    elif isinstance(coverage, dict):
        stock_count = coverage.get("stockCount", 0)
    else:
        raise StockFlowGateError("snapshot_etf_stock_flow_coverage_invalid")
    try:
        has_stock_count = int(stock_count or 0) > 0
    except (TypeError, ValueError) as exc:
        raise StockFlowGateError("snapshot_etf_stock_flow_stock_count_invalid") from exc
    # ``available`` is the declaration; ``complete`` is the stronger
    # declaration used by the workflow's existing readiness rule.
    return available, available and has_stock_count


def _catalog_populated(catalog: dict[str, Any]) -> bool:
    if "items" not in catalog:
        raise StockFlowGateError("stock_flow_catalog_items_missing")
    items = catalog["items"]
    if not isinstance(items, list):
        raise StockFlowGateError("stock_flow_catalog_items_invalid")
    coverage = catalog.get("coverage")
    if coverage is None:
        coverage = {}
    if not isinstance(coverage, dict):
        raise StockFlowGateError("stock_flow_catalog_coverage_invalid")
    try:
        matched_etf_count = int(coverage.get("matchedEtfCount", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise StockFlowGateError("stock_flow_catalog_matched_count_invalid") from exc
    try:
        stock_count = int(coverage.get("stockCount", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise StockFlowGateError("stock_flow_catalog_stock_count_invalid") from exc
    return bool(items) or matched_etf_count > 0 or stock_count > 0


def _history_populated(history: dict[str, Any]) -> bool:
    if "partitions" not in history:
        raise StockFlowGateError("stock_flow_history_partitions_missing")
    partitions = history["partitions"]
    if not isinstance(partitions, list):
        raise StockFlowGateError("stock_flow_history_partitions_invalid")
    return bool(partitions)


def resolve_stock_flow_history(
    snapshot: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    history_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the canonical optional penetration decision.

    ``NOT_REQUIRED`` is the expected result when the snapshot explicitly says
    PCF/penetration is unavailable and the catalog is the empty sidecar that
    the dashboard builder emits.  A populated catalog, a history without its
    counterpart, or an available snapshot without complete artifacts is a
    quality block.  An explicit retryable/quality status remains valid evidence
    and is passed through for the publisher to preserve the dashboard slot.
    """

    declared_available, declared_complete = _declared_stock_flow(snapshot)
    catalog_present = catalog is not None
    history_present = history is not None
    status_present = history_status is not None
    catalog_populated = _catalog_populated(catalog) if catalog_present else False
    history_populated = _history_populated(history) if history_present else False

    status: str | None = None
    if status_present:
        raw_status = history_status.get("status")
        if raw_status not in VALID_HISTORY_STATUSES:
            raise StockFlowGateError("stock_flow_history_status_invalid")
        status = str(raw_status)
        if status == "READY":
            if not catalog_present or not history_present:
                return {
                    "valid": False,
                    "status": "BLOCKED_QUALITY",
                    "reason": "ready_status_missing_artifact",
                }
            if not catalog_populated or not history_populated:
                return {
                    "valid": False,
                    "status": "BLOCKED_QUALITY",
                    "reason": "ready_status_empty_artifact",
                }
            if not declared_complete:
                return {
                    "valid": False,
                    "status": "BLOCKED_QUALITY",
                    "reason": "ready_status_snapshot_not_available",
                }
        elif status == "NOT_REQUIRED" and (catalog_populated or history_populated):
            return {
                "valid": False,
                "status": "BLOCKED_QUALITY",
                "reason": "not_required_status_has_populated_artifact",
            }
        return {
            "valid": True,
            "status": status,
            "reason": "explicit_history_status",
            "declaredAvailable": declared_available,
            "catalogPopulated": catalog_populated,
            "historyPopulated": history_populated,
        }

    # Compatibility for source bundles created before the status-marker
    # rollout: both populated artifacts together are a ready history.
    if catalog_populated and history_populated:
        if not declared_complete:
            return {
                "valid": False,
                "status": "BLOCKED_QUALITY",
                "reason": "history_artifacts_snapshot_not_available",
            }
        return {
            "valid": True,
            "status": "READY",
            "reason": "legacy_complete_artifacts",
            "declaredAvailable": declared_available,
            "catalogPopulated": True,
            "historyPopulated": True,
        }

    if catalog_populated or history_populated:
        return {
            "valid": False,
            "status": "BLOCKED_QUALITY",
            "reason": "incomplete_history_artifacts",
            "declaredAvailable": declared_available,
            "catalogPopulated": catalog_populated,
            "historyPopulated": history_populated,
        }

    if declared_available:
        return {
            "valid": False,
            "status": "BLOCKED_QUALITY",
            "reason": "snapshot_declares_available_but_artifacts_empty",
            "declaredAvailable": True,
            "catalogPopulated": False,
            "historyPopulated": False,
        }

    # An empty catalog sidecar is deliberately not a penetration model.
    return {
        "valid": True,
        "status": "NOT_REQUIRED",
        "reason": "pcf_unavailable_empty_catalog_sidecar",
        "declaredAvailable": declared_available,
        "catalogPopulated": False,
        "historyPopulated": False,
    }
