from __future__ import annotations

import json
from pathlib import Path

from scripts.report_read_model_sizes import build_report


def test_report_records_shape_and_compression(tmp_path: Path) -> None:
    payloads = {
        "bootstrap": {"etfSearch": [{"code": "510300", "name": "沪深300ETF"}]},
        "radar": {"data": {"etfTerminal": {"instruments": []}}},
        "radar_flow": {"data": {"etfStockFlow": None}},
    }
    manifest = {
        "releaseId": "release:2026-08-06:0123456789abcdef",
        "asOf": "2026-08-06",
        "requiredArtifacts": list(payloads),
    }
    directory = tmp_path / "read-models"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, payload in payloads.items():
        (directory / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = build_report(directory)

    assert report["passed"] is True
    assert report["hardFailures"] == []
    assert {entry["name"] for entry in report["artifacts"]} == set(payloads)
    bootstrap = next(entry for entry in report["artifacts"] if entry["name"] == "bootstrap")
    assert bootstrap["rawBytes"] > 0
    assert bootstrap["gzipBytes"] > 0
    assert "etfSearch" in bootstrap["topLevelFieldBytes"]
