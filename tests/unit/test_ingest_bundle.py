from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from trading_desk.ingest_bundle import RETENTION_DAYS, build_ingest_bundle


def test_build_ingest_bundle_verifies_and_serializes_etf_evidence(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/szse/etf_scale/2026-07-15/run.xlsx"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/szse_etf_scale/2026-07-15/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    content = b"official workbook bytes"
    digest = hashlib.sha256(content).hexdigest()
    raw_path.write_bytes(content)
    metadata_path.write_text(
        json.dumps({"sha256": digest, "request_params": {"page_date": "2026-07-16"}}),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "market": "SZSE",
                "instrument_code": "159001",
                "instrument_name": "货币ETF易方达",
                "etf_type": "ETF",
                "investment_type": "货币市场基金",
                "shares": 19_042_471,
                "nav": 100.0,
                "date_basis": "source_page_previous_trading_day",
            }
        ]
    ).to_csv(normalized_path, index=False)
    snapshot_path = tmp_path / "dashboard-snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-07-15"}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run",
                "started_at": "2026-07-16T09:15:00+08:00",
                "finished_at": "2026-07-16T09:16:00+08:00",
                "artifacts": [
                    {
                        "source": {
                            "source_id": "szse",
                            "dataset": "etf_scale",
                            "url": "https://fund.szse.cn/api/report/ShowReport",
                            "date_semantics": "source_page_previous_trading_day",
                        },
                        "requested_date": "2026-07-15",
                        "observed_at": "2026-07-16T09:15:20+08:00",
                        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "raw_path": str(raw_path.relative_to(tmp_path)),
                        "metadata_path": str(metadata_path.relative_to(tmp_path)),
                        "normalized_path": str(normalized_path.relative_to(tmp_path)),
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    read_model_dir = tmp_path / "read-models"
    read_model_dir.mkdir()
    read_model_content = ("{\"etfStockFlow\":\"" + ("x" * 10_000) + "\"}").encode()
    read_model_digest = hashlib.sha256(read_model_content).hexdigest()
    (read_model_dir / "radar.json").write_bytes(read_model_content)
    (read_model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "releaseId": "release:2026-07-15:1234567890abcdef",
                "asOf": "2026-07-15",
                "artifacts": {
                    "radar": {
                        "key": "market-data/releases/2026-07-15/release:2026-07-15:1234567890abcdef/radar.json",
                        "sha256": read_model_digest,
                        "bytes": len(read_model_content),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    bundle = build_ingest_bundle(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
        read_model_dir=read_model_dir,
    )

    assert bundle["archive"]["retentionDays"] == RETENTION_DAYS
    artifact = bundle["archive"]["artifacts"][0]
    assert artifact["releaseId"] == f"etf:szse:2026-07-15:{digest[:16]}"
    assert artifact["metadata"]["request_params"]["page_date"] == "2026-07-16"
    assert artifact["rows"] == [
        {
            "code": "159001",
            "name": "货币ETF易方达",
            "category": "货币市场基金",
                "shares": 19_042_471,
                "navMicros": 100_000_000,
                "navDate": None,
                "dateBasis": "source_page_previous_trading_day",
        }
    ]

    artifact = bundle["readModels"]["artifacts"]["radar"]
    assert artifact["contentEncoding"] == "gzip"
    assert artifact["bytes"] == len(read_model_content)
    assert artifact["sha256"] == read_model_digest
    assert gzip.decompress(base64.b64decode(artifact["contentBase64"])) == read_model_content
