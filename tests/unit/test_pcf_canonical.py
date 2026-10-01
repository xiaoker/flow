from __future__ import annotations

import hashlib

from trading_desk.pcf_canonical import (
    build_deterministic_raw_pack,
    build_pcf_canonical_artifacts,
    verify_deterministic_raw_pack,
)
from trading_desk.pcf_capture import PcfManifestContract


def _build() -> dict[str, object]:
    raw = {"510500": b"<xml code='510500'/>", "510300": b"<xml code='510300'/>"}
    return build_pcf_canonical_artifacts(
        trade_date="2026-08-21",
        previous_date="2026-08-20",
        job_id="pcf:2026-08-21:0123456789abcdef",
        plan_sha256="a" * 64,
        headers=[{"market": "SSE", "etf_code": "510500", "etf_name": "中证500ETF", "source_extra_json": "{}"}],
        components=[{"market": "SSE", "etf_code": "510500", "stock_code": "600000", "quantity": 1, "source_extra_json": "{\"Future\":\"x\"}"}],
        data=[{"trade_date": "2026-08-21", "market": "SSE", "etf_code": "510500", "stock_code": "600000", "quantity": 1}],
        raw_xml=raw,
        required_count=1,
        captured_count=1,
        valid_count=1,
        failed_count=0,
        quality={"pass": 3, "warn": 0, "fail": 0},
        observed_at="2026-08-21T00:00:00Z",
    )


def test_canonical_and_pack_are_byte_deterministic() -> None:
    first = _build()
    second = _build()
    assert first["manifestBytes"] == second["manifestBytes"]
    assert first["manifestSha256"] == second["manifestSha256"]
    assert first["artifacts"]["rawPack"]["bytes"] == second["artifacts"]["rawPack"]["bytes"]
    pack = first["artifacts"]["rawPack"]
    assert first["manifest"]["qualification"]["status"] == "QUALIFIED"
    assert first["manifest"]["qualification"]["evidence"]["rawPackSha256"] == pack["sha256"]
    assert first["manifest"]["sourceWindow"]["targetDate"] == "2026-08-21"
    verify_deterministic_raw_pack(pack["bytes"], {"members": [{"etfCode": code, "file": f"{code}.xml", "bytes": len(content), "rawSha256": hashlib.sha256(content).hexdigest()} for code, content in (("510300", b"<xml code='510300'/>"), ("510500", b"<xml code='510500'/>"))]})


def test_partial_finalize_is_not_ready_and_manifest_contract_is_valid() -> None:
    result = _build()
    partial = build_pcf_canonical_artifacts(
        trade_date="2026-08-21", previous_date="2026-08-20", job_id="pcf:2026-08-21:partial",
        plan_sha256="b" * 64, headers=[], components=[], data=[], raw_xml={"510500": b"x"},
        required_count=2, captured_count=1, valid_count=1, failed_count=0,
    )
    assert partial["ready"] is False
    assert PcfManifestContract.model_validate(result["manifest"])
