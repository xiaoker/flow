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
    assert len(artifact["canonicalSha256"]) == 64
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

    first_run_id = bundle["archive"]["runId"]
    assert first_run_id.startswith("bundle:2026-07-15:")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["run_id"] = "a-different-wall-clock-run"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    repeated = build_ingest_bundle(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
        read_model_dir=read_model_dir,
    )
    assert repeated["archive"]["runId"] == first_run_id


def test_ingest_run_id_ignores_transport_only_raw_response_changes(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/sse/etf_scale/2026-07-15/run.json"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/sse_etf_scale/2026-07-15/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    normalized = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "market": "SSE",
                "instrument_code": "510300",
                "instrument_name": "沪深300ETF",
                "investment_type": "股票ETF",
                "shares": 100,
                "date_basis": "source_published",
            }
        ]
    )
    normalized.to_csv(normalized_path, index=False)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-07-15"}), encoding="utf-8")

    def write_manifest(raw: bytes, run_id: str) -> Path:
        raw_path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
        manifest_path = tmp_path / f"manifest-{run_id}.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "started_at": "2026-07-16T09:15:00+08:00",
                    "finished_at": "2026-07-16T09:16:00+08:00",
                    "artifacts": [
                        {
                            "source": {
                                "source_id": "sse",
                                "dataset": "etf_scale",
                                "url": "https://query.sse.com.cn/commonQuery.do",
                                "date_semantics": "source_published",
                            },
                            "requested_date": "2026-07-15",
                            "observed_at": "2026-07-16T09:15:20+08:00",
                            "content_type": "application/json",
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
        return manifest_path

    first = build_ingest_bundle(
        snapshot_path=snapshot_path,
        manifest_path=write_manifest(b'{"requestId":"one"}', "run-one"),
        workspace_root=tmp_path,
    )
    second = build_ingest_bundle(
        snapshot_path=snapshot_path,
        manifest_path=write_manifest(b'{"requestId":"two"}', "run-two"),
        workspace_root=tmp_path,
    )

    assert first["archive"]["artifacts"][0]["sha256"] != second["archive"]["artifacts"][0]["sha256"]
    assert first["archive"]["runId"] == second["archive"]["runId"]
