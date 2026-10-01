from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from trading_desk.etf_flow_state import materialize_etf_terminal
from trading_desk.read_model_capabilities import (
    CAPABILITY_ARTIFACTS,
    CAPABILITY_CONTRACT_VERSION,
    CAPABILITY_IDS,
    ETF_PERIOD_IDS,
    ETF_PERIOD_METRICS,
    build_capability_states,
)
from trading_desk.read_models import build_read_models
from scripts.split_read_model_slots import SLOT_ARTIFACTS, split


def _state() -> dict[str, Any]:
    return {
        "version": 1,
        "classificationVersion": "2026-07-v3",
        "dates": ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"],
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
                "values": [0.5, 1.0, 1.5, -0.5],
                "cashValues": [1.0, 2.0, 3.0, -1.0],
            },
        ],
    }


def _snapshot() -> dict[str, Any]:
    return {
        "asOf": "2026-08-06",
        "observedAt": "2026-08-06T09:00:00+08:00",
        "mode": "官方数据快照",
        "gate": "conditional_pass",
        "quality": {"pass": 1, "warn": 0, "fail": 0},
        "publication": {"status": "confirmed"},
        "metrics": [],
        "etfFlows": {"inflow": [], "outflow": []},
        "marginSeries": [],
        "connect": [],
        "dataStatus": [
            {"name": "上交所 ETF", "status": "ready", "asOf": "2026-08-06"},
            {"name": "深交所 ETF", "status": "ready", "asOf": "2026-08-06"},
            {"name": "融资融券", "status": "ready", "asOf": "2026-08-06"},
            {"name": "互联互通", "status": "ready", "asOf": "2026-08-06"},
        ],
        "etfTerminal": materialize_etf_terminal(_state(), base_terminal={}),
        "etfStockFlow": {
            "available": False,
            "basis": {"shareDate": "2026-08-06", "pcfDate": None, "pcfAlignment": None},
            "coverage": {"matchedEtfCount": 0},
            "stocks": [],
        },
    }


def _empty_stock_catalog() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "asOf": "2026-08-06",
        "basis": {"shareDate": "2026-08-06", "pcfDate": None},
        "coverage": {"stockCount": 0, "matchedEtfCount": 0},
        "items": [],
        "warnings": ["aligned_pcf_missing"],
    }


def _artifact_content(payloads: dict[str, dict[str, Any]], name: str) -> bytes:
    return json.dumps(payloads[name], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_capability_contract_is_deterministic_and_core_is_independent_of_pcf() -> None:
    snapshot = _snapshot()
    payloads_a, manifest_a = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    payloads_b, manifest_b = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    _, legacy_manifest = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
    )

    assert manifest_a == manifest_b
    assert payloads_a == payloads_b
    assert manifest_a["releaseId"] != legacy_manifest["releaseId"]
    assert manifest_a["schemaVersion"] == 4
    contract = manifest_a["capabilityContract"]
    assert contract["scope"] == "full"
    assert set(contract["capabilities"]) == set(CAPABILITY_IDS)
    assert contract["capabilities"]["etf_daily_core"]["status"] == "READY"
    assert contract["capabilities"]["etf_penetration"]["status"] == "SOURCE_UNAVAILABLE"
    assert contract["capabilities"]["stock_search"]["status"] == "SOURCE_UNAVAILABLE"
    assert contract["capabilities"]["stock_detail"]["status"] == "SOURCE_UNAVAILABLE"
    assert contract["capabilities"]["etf_periods"]["periods"].keys() == set(ETF_PERIOD_IDS)
    assert all(
        set(contract["capabilities"]["etf_periods"]["periods"][period]) == set(ETF_PERIOD_METRICS)
        for period in ETF_PERIOD_IDS
    )
    assert contract["capabilities"]["etf_daily_core"]["artifactRefs"] == [
        {
            "name": name,
            "key": manifest_a["artifacts"][name]["key"],
            "sha256": manifest_a["artifacts"][name]["sha256"],
            "bytes": manifest_a["artifacts"][name]["bytes"],
        }
        for name in CAPABILITY_ARTIFACTS["etf_daily_core"]
    ]


def test_capability_contract_never_marks_wrong_exchange_date_ready() -> None:
    snapshot = _snapshot()
    snapshot["dataStatus"][1]["asOf"] = "2026-08-05"
    _, manifest = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    assert manifest["capabilityContract"]["capabilities"]["etf_daily_core"]["status"] == "SOURCE_UNAVAILABLE"
    assert manifest["capabilityContract"]["capabilities"]["etf_radar"]["status"] == "WAITING_SOURCES"


def test_invalid_declared_period_coverage_fails_closed() -> None:
    snapshot = _snapshot()
    snapshot["etfTerminal"]["instruments"][0]["periodCoverage"]["1d"] = 100
    _, manifest = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    period_state = manifest["capabilityContract"]["capabilities"]["etf_periods"]
    assert period_state["status"] == "UNKNOWN"
    assert period_state["reasonCode"] == "period_coverage_evidence_invalid"
    assert period_state["periods"]["1d"]["shares"]["status"] == "UNKNOWN"


def test_empty_independent_capabilities_are_not_marked_ready() -> None:
    snapshot = _snapshot()
    for item, code in zip(snapshot["etfTerminal"]["instruments"], ("512999", "159999"), strict=True):
        item["code"] = code
    payloads, manifest = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    payloads["rotation"]["data"]["groupPeriods"] = []
    states = build_capability_states(
        scope="full",
        as_of=snapshot["asOf"],
        source_dates=manifest["sourceDates"],
        snapshot=snapshot,
        payloads=payloads,
        stock_catalog=_empty_stock_catalog(),
        artifact_names=set(manifest["artifacts"]),
    )

    assert states["etf_daily_core"]["status"] == "READY"
    assert states["national_team"]["status"] == "MISSING"
    assert states["national_team"]["coverage"]["sampleCount"] == 0
    assert states["rotation"]["status"] == "MISSING"
    assert states["rotation"]["coverage"]["sampleCount"] == 0


def test_v4_slot_split_rebinds_exact_artifacts_and_keeps_other_slot_unknown(tmp_path: Path) -> None:
    payloads, manifest = build_read_models(
        _snapshot(),
        require_materialized=True,
        stock_catalog=_empty_stock_catalog(),
        capability_contract_version=CAPABILITY_CONTRACT_VERSION,
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in manifest["artifacts"]:
        content = _artifact_content(payloads, name)
        assert hashlib.sha256(content).hexdigest() == manifest["artifacts"][name]["sha256"]
        (source / f"{name}.json").write_bytes(content)
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    dashboard = split(source, tmp_path / "dashboard", "dashboard")
    dashboard_repeat = split(source, tmp_path / "dashboard-repeat", "dashboard")
    penetration = split(source, tmp_path / "penetration", "etf_penetration")

    assert dashboard["releaseId"] == dashboard_repeat["releaseId"]
    assert dashboard["manifestSha256"] == dashboard_repeat["manifestSha256"]
    assert dashboard["schemaVersion"] == penetration["schemaVersion"] == 4
    assert dashboard["capabilityContract"]["capabilities"]["etf_daily_core"]["status"] == "READY"
    assert dashboard["capabilityContract"]["capabilities"]["etf_penetration"] == {
        "status": "UNKNOWN",
        "asOf": None,
        "sourceDates": {},
        "reasonCode": "separate_slot_not_bound",
        "coverage": {},
        "artifactRefs": [],
    }
    assert penetration["capabilityContract"]["capabilities"]["etf_daily_core"]["status"] == "UNKNOWN"
    for slot_manifest in (dashboard, penetration):
        for capability_id, state in slot_manifest["capabilityContract"]["capabilities"].items():
            expected_names = CAPABILITY_ARTIFACTS[capability_id] if state["status"] != "UNKNOWN" or state["reasonCode"] != "separate_slot_not_bound" else ()
            for ref, name in zip(state["artifactRefs"], expected_names, strict=True):
                assert ref["name"] == name
                assert ref["key"] == slot_manifest["artifacts"][name]["key"]
                assert ref["sha256"] == slot_manifest["artifacts"][name]["sha256"]
    assert dashboard["requiredArtifacts"] == list(SLOT_ARTIFACTS["dashboard"])
    assert penetration["requiredArtifacts"] == list(SLOT_ARTIFACTS["etf_penetration"])

    damaged_source = tmp_path / "damaged-source"
    shutil.copytree(source, damaged_source)
    damaged_manifest = json.loads((damaged_source / "manifest.json").read_text(encoding="utf-8"))
    damaged_manifest["capabilityContract"]["capabilities"]["etf_daily_core"]["artifactRefs"][0]["key"] += "/wrong"
    (damaged_source / "manifest.json").write_text(json.dumps(damaged_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source capability artifact reference mismatch"):
        split(damaged_source, tmp_path / "rejected", "dashboard")

    damaged_ready = tmp_path / "damaged-ready-source"
    shutil.copytree(source, damaged_ready)
    damaged_manifest = json.loads((damaged_ready / "manifest.json").read_text(encoding="utf-8"))
    damaged_manifest["capabilityContract"]["capabilities"]["rotation"]["coverage"]["sampleCount"] = 0
    (damaged_ready / "manifest.json").write_text(json.dumps(damaged_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source READY rotation proof is invalid"):
        split(damaged_ready, tmp_path / "rejected-ready", "etf_penetration")

    damaged_period = tmp_path / "damaged-period-source"
    shutil.copytree(source, damaged_period)
    damaged_manifest = json.loads((damaged_period / "manifest.json").read_text(encoding="utf-8"))
    damaged_manifest["capabilityContract"]["capabilities"]["etf_periods"]["periods"]["1d"]["shares"]["availableSampleCount"] = 999
    (damaged_period / "manifest.json").write_text(json.dumps(damaged_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source etf_periods evidence is inconsistent"):
        split(damaged_period, tmp_path / "rejected-period", "etf_penetration")

    damaged_other_artifact = tmp_path / "damaged-other-slot-artifact"
    shutil.copytree(source, damaged_other_artifact)
    rotation_path = damaged_other_artifact / "rotation.json"
    rotation_path.write_bytes(rotation_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="source artifact digest mismatch: rotation"):
        split(damaged_other_artifact, tmp_path / "rejected-other-slot", "etf_penetration")
