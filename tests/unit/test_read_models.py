from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk.etf_flow_state import materialize_etf_terminal
from trading_desk.read_models import ReadModelError, build_read_models, stock_shard_name, write_read_models


def _state() -> dict:
    return {
        "version": 1,
        "classificationVersion": "2026-07-v3",
        "dates": ["2026-08-01", "2026-08-04", "2026-08-05", "2026-08-06"],
        "instruments": [
            {
                "code": "510300",
                "name": "沪深300ETF华泰柏瑞",
                "market": "SSE",
                "category": "宽基",
                "groupType": "broad",
                "assetClass": "股票",
                "values": [1.0, 2.0, -1.0, 3.0],
                "cashValues": [2.0, 4.0, -2.0, 6.0],
            },
            {
                "code": "159915",
                "name": "创业板ETF易方达",
                "market": "SZSE",
                "category": "宽基",
                "groupType": "broad",
                "assetClass": "股票",
                "values": [0.5, None, 1.5, -0.5],
                "cashValues": [1.0, None, 3.0, -1.0],
            },
        ],
    }


def _snapshot() -> dict:
    state = _state()
    terminal = materialize_etf_terminal(state, base_terminal={})
    return {
        "asOf": "2026-08-06",
        "observedAt": "2026-08-06T09:00:00+08:00",
        "mode": "官方数据快照",
        "gate": "conditional_pass",
        "quality": {"pass": 1, "warn": 0, "fail": 0},
        "metrics": [],
        "etfFlows": {"inflow": [], "outflow": []},
        "marginSeries": [],
        "connect": [],
        "dataStatus": [],
        "etfTerminal": terminal,
    }


def _stock_catalog() -> dict:
    return {
        "schemaVersion": 1,
        "asOf": "2026-08-06",
        "basis": {
            "shareDate": "2026-08-06",
            "previousShareDate": "2026-08-05",
            "pcfDate": "2026-08-06",
            "pcfAlignment": "same_trading_day",
            "calculation": "ETF份额变化 ÷ 最小申赎单位 × PCF成分数量",
            "source": "test",
        },
        "coverage": {"stockCount": 1, "matchedEtfCount": 1},
        "items": [
            {
                "listingMarket": "101",
                "listingMarketName": "上交所",
                "stockCode": "600000",
                "stockName": "浦发银行",
                "netShares": 123.0,
                "inflowShares": 123.0,
                "outflowShares": 0.0,
                "etfCount": 1,
                "contributorCount": 1,
                "shownContributorCount": 1,
                "contributorsLimited": False,
                "contributors": [
                    {
                        "etfMarket": "SSE",
                        "etfCode": "510300",
                        "etfName": "沪深300ETF",
                        "shareDelta": 1.0,
                        "quantity": 100,
                        "creationUnit": 1000000,
                        "stockFlowShares": 123.0,
                        "currentPenetratedShares": 1234.0,
                        "cashSubstitution": False,
                        "estimated": True,
                    }
                ],
            }
        ],
        "warnings": [],
    }


def test_materializer_builds_periods_groups_and_coverage() -> None:
    terminal = materialize_etf_terminal(_state(), base_terminal={})

    assert terminal["historyCoverage"] == {
        "from": "2026-08-01",
        "to": "2026-08-06",
        "tradingDays": 4,
        "stateVersion": 1,
    }
    assert terminal["availablePeriods"] == ["1d", "3d"]
    first = next(item for item in terminal["instruments"] if item["code"] == "510300")
    assert first["periods"]["1d"] == 3.0
    assert first["periods"]["3d"] == 4.0
    second = next(item for item in terminal["instruments"] if item["code"] == "159915")
    assert second["periods"]["3d"] is None
    assert second["periodCoverage"]["3d"] == 2
    assert terminal["groupPeriods"][0]["periods"]["1d"] == 2.5


def test_read_model_manifest_is_deterministic_and_split() -> None:
    payloads_a, manifest_a = build_read_models(_snapshot(), require_materialized=True)
    payloads_b, manifest_b = build_read_models(_snapshot(), require_materialized=True)

    assert manifest_a == manifest_b
    assert set(payloads_a) == {"bootstrap", "overview", "radar", "rotation", "national", "connect", "margin"}
    assert "etfTerminal" not in payloads_a["overview"]["data"]
    assert payloads_a["radar"]["data"]["etfTerminal"]["historyCoverage"]["tradingDays"] == 4
    assert manifest_a["releaseId"].startswith("release:2026-08-06:")


def test_stock_catalog_is_split_into_deterministic_search_and_shards() -> None:
    catalog = _stock_catalog()
    snapshot = _snapshot()
    snapshot["marginSeries"] = [{"date": "08-06", "balance": 22400.0}]
    payloads_a, manifest_a = build_read_models(snapshot, require_materialized=True, stock_catalog=catalog)
    payloads_b, manifest_b = build_read_models(snapshot, require_materialized=True, stock_catalog=catalog)

    assert manifest_a == manifest_b
    assert "stock_search" in payloads_a
    assert "pressure" in payloads_a
    assert payloads_a["pressure"]["data"]["pressure"]["summary"]["marketFinancingBalanceYuan"] == 2_240_000_000_000.0
    assert {f"stock_{index}" for index in range(8)} <= set(payloads_a)
    search_item = payloads_a["stock_search"]["items"][0]
    assert search_item["shard"] == stock_shard_name("101", "600000")
    assert payloads_a[search_item["shard"]]["items"][0]["stockCode"] == "600000"
    assert all(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 5_000_000 for payload in payloads_a.values())
    assert set(manifest_a["artifacts"]) == {
        "bootstrap", "overview", "radar", "rotation", "national", "connect", "margin",
        "pressure",
        "stock_search", *{f"stock_{index}" for index in range(8)},
    }


def test_stock_catalog_rejects_duplicate_identity() -> None:
    catalog = _stock_catalog()
    catalog["items"].append(dict(catalog["items"][0]))
    with pytest.raises(ReadModelError, match="stock_catalog_duplicate_identity"):
        build_read_models(_snapshot(), require_materialized=True, stock_catalog=catalog)


def test_materialized_contract_rejects_legacy_runtime_hydration_snapshot() -> None:
    snapshot = _snapshot()
    snapshot["etfTerminal"].pop("historyCoverage")
    with pytest.raises(ReadModelError, match="etf_history_coverage_missing"):
        build_read_models(snapshot, require_materialized=True)


def test_stock_flow_read_model_rejects_cross_day_pcf() -> None:
    snapshot = _snapshot()
    snapshot["etfStockFlow"] = {
        "available": True,
        "basis": {
            "shareDate": "2026-08-06",
            "previousShareDate": "2026-08-05",
            "pcfDate": "2026-08-05",
        },
        "coverage": {},
        "stocks": [],
    }
    with pytest.raises(ReadModelError, match="etf_stock_flow_date_mismatch"):
        build_read_models(snapshot, require_materialized=True)


def test_available_stock_flow_requires_complete_stock_sidecars() -> None:
    snapshot = _snapshot()
    snapshot["etfStockFlow"] = {
        "available": True,
        "basis": {
            "shareDate": "2026-08-06",
            "previousShareDate": "2026-08-05",
            "pcfDate": "2026-08-06",
            "pcfAlignment": "same_trading_day",
        },
        "coverage": {"matchedEtfCount": 1},
        "stocks": [{"stockCode": "600522", "stockName": "中天科技"}],
    }
    with pytest.raises(ReadModelError, match="stock_catalog_required_for_available_flow"):
        build_read_models(snapshot, require_materialized=True)


def test_write_read_models_uses_canonical_json(tmp_path: Path) -> None:
    result = write_read_models(_snapshot(), tmp_path, require_materialized=True)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifestSha256"] == result["manifest"]["manifestSha256"]
    assert (tmp_path / "radar.json").read_bytes() == (tmp_path / "radar.json").read_bytes()
