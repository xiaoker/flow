from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from trading_desk.history_bundle import (
    HISTORY_RETENTION_DAYS,
    build_history_bundle_from_files,
    serialize_history_artifact,
)


def test_history_bundle_preserves_raw_and_normalized_hashes(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/hkex/stock_connect_daily/2026-07-21/run.js"
    metadata_path = raw_path.with_name("run.metadata.json")
    summary_path = tmp_path / "data/normalized/hkex_summary/2026-07-21/run.csv"
    top_path = tmp_path / "data/normalized/hkex_top/2026-07-21/run.csv"
    raw_path.parent.mkdir(parents=True)
    summary_path.parent.mkdir(parents=True)
    top_path.parent.mkdir(parents=True)
    raw = b"tabData = [];"
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.write_bytes(raw)
    metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    pd.DataFrame(
        [{"trade_date": "2026-07-21", "market": "SSE", "direction": "northbound"}]
    ).to_csv(summary_path, index=False)
    pd.DataFrame(
        [{"trade_date": "2026-07-21", "market": "SSE", "rank": 1}]
    ).to_csv(top_path, index=False)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-07-21"}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "history-test",
                "started_at": "2026-07-22T09:15:00+08:00",
                "finished_at": "2026-07-22T09:16:00+08:00",
                "artifacts": [
                    {
                        "source": {
                            "source_id": "hkex",
                            "dataset": "stock_connect_daily",
                            "url": "https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_[YYYY][MM][DD]c.js",
                            "date_semantics": "source_published",
                        },
                        "requested_date": "2026-07-21",
                        "observed_at": "2026-07-22T09:15:20+08:00",
                        "content_type": "application/javascript",
                        "raw_path": str(raw_path.relative_to(tmp_path)),
                        "metadata_path": str(metadata_path.relative_to(tmp_path)),
                        "normalized_path": (
                            f"{summary_path.relative_to(tmp_path)};{top_path.relative_to(tmp_path)}"
                        ),
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    bundle = build_history_bundle_from_files(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
    )

    assert bundle["history"]["retentionDays"] == HISTORY_RETENTION_DAYS
    artifact = bundle["history"]["artifacts"][0]
    assert artifact["releaseId"].startswith("stock_connect_daily:hkex:2026-07-21:")
    assert artifact["tradeDates"] == ["2026-07-21"]
    assert [item["name"] for item in artifact["normalized"]] == ["summary", "top"]
    assert artifact["normalized"][0]["rowCount"] == 1


def test_history_bundle_carries_stable_pipeline_run_key(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/hkex/stock_connect_daily/2026-07-21/run.csv"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/hkex_summary/2026-07-21/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    raw = b"official,bytes\n"
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.write_bytes(raw)
    metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    pd.DataFrame([{"trade_date": "2026-07-21", "market": "SSE", "direction": "northbound"}]).to_csv(
        normalized_path, index=False
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-07-21"}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run-key-test",
                "started_at": "2026-07-21T17:00:00+08:00",
                "finished_at": "2026-07-21T17:01:00+08:00",
                "artifacts": [
                    {
                        "source": {
                            "source_id": "hkex",
                            "dataset": "stock_connect_daily",
                            "url": "https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_[YYYY][MM][DD]c.js",
                            "date_semantics": "source_published",
                        },
                        "requested_date": "2026-07-21",
                        "observed_at": "2026-07-21T17:00:10+08:00",
                        "content_type": "text/csv",
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

    bundle = build_history_bundle_from_files(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
        run_key="daily-market-data:2026-07-21:evening:v1",
    )

    assert bundle["history"]["runKey"] == "daily-market-data:2026-07-21:evening:v1"


def test_history_bundle_compresses_wide_pcf_in_transit(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/sse/etf_pcf/2026-07-21/run.gz"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/sse_etf_pcf/2026-07-21/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    raw = gzip.compress(b'{"responses":[]}')
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.write_bytes(raw)
    metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    pd.DataFrame(
        [
            {
                "trade_date": "2026-07-21",
                "market": "SSE",
                "etf_code": "588000",
                "stock_code": "688008",
                "quantity": 1813,
                "creation_unit": 3000000,
            }
        ]
    ).to_csv(normalized_path, index=False)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-07-21"}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "pcf-history-test",
                "started_at": "2026-07-22T09:15:00+08:00",
                "finished_at": "2026-07-22T09:16:00+08:00",
                "artifacts": [
                    {
                        "source": {
                            "source_id": "sse",
                            "dataset": "etf_pcf",
                            "url": "https://query.sse.com.cn/commonQuery.do",
                            "date_semantics": "sse_pcf_same_day_morning",
                        },
                        "requested_date": "2026-07-21",
                        "observed_at": "2026-07-22T09:15:20+08:00",
                        "content_type": "application/gzip",
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

    bundle = build_history_bundle_from_files(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
    )
    normalized = bundle["history"]["artifacts"][0]["normalized"][0]
    decoded = gzip.decompress(base64.b64decode(normalized["contentBase64"]))

    assert normalized["contentEncoding"] == "gzip"
    assert hashlib.sha256(decoded).hexdigest() == normalized["sha256"]
    assert b"588000" in decoded


def test_history_bundle_accepts_bse_margin_source(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/bse/margin_market_daily/2026-08-17/run.json"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/bse_margin/2026-08-17/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    raw = b"null([], '2026-08-17')"
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.write_bytes(raw)
    metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    pd.DataFrame(
        [{
            "trade_date": "2026-08-17",
            "market": "BSE",
            "financing_balance_yuan": 8_210_630_112,
        }]
    ).to_csv(normalized_path, index=False)

    artifact = serialize_history_artifact(
        {
            "source": {
                "source_id": "bse",
                "dataset": "margin_market_daily",
                "url": "https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do",
                "date_semantics": "bse_official_summary_trade_date",
            },
            "requested_date": "2026-08-17",
            "observed_at": "2026-08-18T09:00:00+08:00",
            "content_type": "application/json",
            "raw_path": str(raw_path.relative_to(tmp_path)),
            "metadata_path": str(metadata_path.relative_to(tmp_path)),
            "normalized_path": str(normalized_path.relative_to(tmp_path)),
            "sha256": digest,
        },
        workspace_root=tmp_path,
    )

    assert artifact["releaseId"].startswith("margin_market_daily:bse:2026-08-17:")
    assert artifact["tradeDates"] == ["2026-08-17"]
    assert artifact["normalized"][0]["rowCount"] == 1


def test_history_bundle_accepts_sse_margin_security_source(tmp_path: Path) -> None:
    raw_path = tmp_path / "data/raw/sse/margin_security_daily/2026-08-17/run.json"
    metadata_path = raw_path.with_name("run.metadata.json")
    normalized_path = tmp_path / "data/normalized/sse_margin_security/2026-08-17/run.csv"
    raw_path.parent.mkdir(parents=True)
    normalized_path.parent.mkdir(parents=True)
    raw = b'{"pageHelp":{"data":[]}}'
    digest = hashlib.sha256(raw).hexdigest()
    raw_path.write_bytes(raw)
    metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    pd.DataFrame(
        [{
            "trade_date": "2026-08-17",
            "source_trade_date": "2026-08-17",
            "market": "SSE",
            "stock_code": "600000",
            "security_name": "浦发银行",
            "financing_balance_yuan": 123456789,
            "financing_buy_yuan": 2345678,
            "financing_repayment_yuan": 345678,
            "securities_lending_balance_shares": 456789,
            "securities_lending_sell_shares": 56789,
            "securities_lending_repayment_shares": 6789,
            "source_id": "sse",
            "source_grade": "A",
            "date_basis": "sse_margin_security_report_date",
            "observed_at": "2026-08-18T09:00:00+08:00",
        }]
    ).to_csv(normalized_path, index=False)

    artifact = serialize_history_artifact(
        {
            "source": {
                "source_id": "sse",
                "dataset": "margin_security_daily",
                "url": "https://query.sse.com.cn/commonSoaQuery.do",
                "date_semantics": "sse_margin_security_report_date",
            },
            "requested_date": "2026-08-17",
            "observed_at": "2026-08-18T09:00:00+08:00",
            "content_type": "application/json",
            "raw_path": str(raw_path.relative_to(tmp_path)),
            "metadata_path": str(metadata_path.relative_to(tmp_path)),
            "normalized_path": str(normalized_path.relative_to(tmp_path)),
            "sha256": digest,
        },
        workspace_root=tmp_path,
    )

    assert artifact["releaseId"].startswith("margin_security_daily:sse:2026-08-17:")
    assert artifact["tradeDates"] == ["2026-08-17"]
    assert artifact["normalized"][0]["rowCount"] == 1


def test_history_bundle_generates_offline_etf_adjacent_changes_from_canonical_parts(
    tmp_path: Path,
) -> None:
    dates = ["2026-08-21", "2026-08-22"]
    artifacts: list[dict[str, object]] = []
    for source_id, code, previous, current, host in [
        ("sse", "510300", 100, 130, "query.sse.com.cn"),
        ("szse", "159919", 200, 250, "www.szse.cn"),
    ]:
        raw_path = tmp_path / f"data/raw/{source_id}/etf_scale/2026-08-22/{source_id}.json"
        metadata_path = raw_path.with_name(f"{source_id}.metadata.json")
        normalized_path = tmp_path / f"data/normalized/{source_id}_etf_scale/2026-08-22/{source_id}.csv"
        raw_path.parent.mkdir(parents=True)
        normalized_path.parent.mkdir(parents=True)
        raw = f"{{\"source\":\"{source_id}\"}}".encode()
        digest = hashlib.sha256(raw).hexdigest()
        raw_path.write_bytes(raw)
        metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
        pd.DataFrame(
            [
                {
                    "trade_date": dates[0],
                    "market": "SSE" if source_id == "sse" else "SZSE",
                    "instrument_code": code,
                    "instrument_name": "测试ETF",
                    "shares": previous,
                },
                {
                    "trade_date": dates[1],
                    "market": "SSE" if source_id == "sse" else "SZSE",
                    "instrument_code": code,
                    "instrument_name": "测试ETF",
                    "shares": current,
                },
                # The same collection window can also contain a newer source
                # response than the snapshot selected for publication.  It
                # must remain archive evidence but cannot enter an as-of
                # bounded adjacent-change contract.
                {
                    "trade_date": "2026-08-23",
                    "market": "SSE" if source_id == "sse" else "SZSE",
                    "instrument_code": code,
                    "instrument_name": "测试ETF",
                    "shares": current + 10,
                },
            ]
        ).to_csv(normalized_path, index=False)
        artifacts.append(
            {
                "source": {
                    "source_id": source_id,
                    "dataset": "etf_scale",
                    "url": f"https://{host}/etf-scale",
                    "date_semantics": "source_published",
                },
                "requested_date": "2026-08-22",
                "observed_at": "2026-08-22T18:00:00+08:00",
                "content_type": "application/json",
                "raw_path": str(raw_path.relative_to(tmp_path)),
                "metadata_path": str(metadata_path.relative_to(tmp_path)),
                "normalized_path": str(normalized_path.relative_to(tmp_path)),
                "sha256": digest,
            }
        )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": dates[1]}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "adjacent-history-test",
                "started_at": "2026-08-22T18:00:00+08:00",
                "finished_at": "2026-08-22T18:01:00+08:00",
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )

    bundle = build_history_bundle_from_files(
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        workspace_root=tmp_path,
    )

    adjacent = bundle["history"]["officialEtfDailyChanges"]
    assert adjacent["schemaVersion"] == 1
    assert adjacent["asOf"] == dates[1]
    assert adjacent["expectedPreviousDate"] == dates[0]
    assert adjacent["sourceReleaseIds"] == [
        next(item["releaseId"] for item in bundle["history"]["artifacts"] if item["sourceId"] == "sse"),
        next(item["releaseId"] for item in bundle["history"]["artifacts"] if item["sourceId"] == "szse"),
    ]
    assert {(row["market"], row["code"], row["changeShares"]) for row in adjacent["rows"]} == {
        ("SSE", "510300", 30),
        ("SZSE", "159919", 50),
    }
