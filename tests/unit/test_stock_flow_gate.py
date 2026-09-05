from __future__ import annotations

import pytest

from trading_desk.stock_flow_gate import StockFlowGateError, resolve_stock_flow_history


def _snapshot(*, available: bool = False, stock_count: int = 0) -> dict:
    return {
        "publication": {"status": "confirmed"},
        "etfStockFlow": {
            "available": available,
            "coverage": {"stockCount": stock_count},
        },
    }


def _catalog(*, populated: bool) -> dict:
    return {
        "items": [{"etfCode": "588000"}] if populated else [],
        "coverage": {
            "matchedEtfCount": 1 if populated else 0,
            "stockCount": 1 if populated else 0,
        },
    }


def _history(*, populated: bool) -> dict:
    return {"partitions": [{"date": "2026-09-03"}] if populated else []}


def _status(value: str) -> dict:
    return {"status": value}


def test_empty_unavailable_catalog_sidecar_is_not_a_quality_failure() -> None:
    result = resolve_stock_flow_history(
        _snapshot(), catalog=_catalog(populated=False)
    )

    assert result["valid"] is True
    assert result["status"] == "NOT_REQUIRED"
    assert result["reason"] == "pcf_unavailable_empty_catalog_sidecar"


def test_ready_history_requires_snapshot_and_both_populated_artifacts() -> None:
    result = resolve_stock_flow_history(
        _snapshot(available=True, stock_count=1),
        catalog=_catalog(populated=True),
        history=_history(populated=True),
        history_status=_status("READY"),
    )

    assert result["valid"] is True
    assert result["status"] == "READY"


def test_populated_catalog_without_history_is_blocked() -> None:
    result = resolve_stock_flow_history(
        _snapshot(available=True, stock_count=1),
        catalog=_catalog(populated=True),
    )

    assert result["valid"] is False
    assert result["status"] == "BLOCKED_QUALITY"
    assert result["reason"] == "incomplete_history_artifacts"


def test_available_snapshot_without_artifacts_is_blocked() -> None:
    result = resolve_stock_flow_history(_snapshot(available=True, stock_count=1))

    assert result["valid"] is False
    assert result["reason"] == "snapshot_declares_available_but_artifacts_empty"


def test_explicit_retryable_status_is_preserved_for_dashboard_publication() -> None:
    result = resolve_stock_flow_history(
        _snapshot(), history_status=_status("FAILED_RETRYABLE")
    )

    assert result["valid"] is True
    assert result["status"] == "FAILED_RETRYABLE"


def test_legacy_complete_artifacts_remain_compatible() -> None:
    result = resolve_stock_flow_history(
        _snapshot(available=True, stock_count=1),
        catalog=_catalog(populated=True),
        history=_history(populated=True),
    )

    assert result["valid"] is True
    assert result["status"] == "READY"


def test_malformed_present_artifact_fails_closed() -> None:
    with pytest.raises(StockFlowGateError, match="stock_flow_catalog_items_missing"):
        resolve_stock_flow_history(
            _snapshot(), catalog={"coverage": {"matchedEtfCount": 0}}
        )
