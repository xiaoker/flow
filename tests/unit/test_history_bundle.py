from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.history_bundle import (
    HISTORY_RETENTION_DAYS,
    build_history_bundle_from_files,
    serialize_history_artifact,
)


def test_sources_only_bundle_survives_missing_snapshot_and_adjacent_failure(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from trading_desk.history_bundle import build_source_history_bundle_from_files
    test_history_bundle_preserves_raw_and_normalized_hashes(tmp_path)
    manifest = tmp_path / "manifest.json"
    value = json.loads(manifest.read_bytes()); value["requested_dates"] = ["2026-07-21"]
    manifest.write_text(json.dumps(value))
    for path in (tmp_path / "data/normalized").rglob("*.csv"):
        frame = pd.read_csv(path)
        frame["source_id"] = "hkex"; frame["observed_at"] = "2026-07-22T09:15:20+08:00"
        if "hkex_summary" in str(path):
            frame["currency"] = "RMB"; frame["total_turnover"] = 100; frame["etf_turnover"] = 10
        frame.to_csv(path, index=False)
    (tmp_path / "snapshot.json").unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("source preservation must not calculate adjacent changes")
    monkeypatch.setattr("trading_desk.history_bundle._build_official_etf_daily_changes_from_artifacts", forbidden)
    bundle = build_source_history_bundle_from_files(manifest_path=manifest, workspace_root=tmp_path)
    assert bundle["history"]["finalize"] is False
    assert "snapshot" not in bundle["history"] and "officialEtfDailyChanges" not in bundle["history"]
    assert bundle["history"]["asOf"] == "2026-07-21" and len(bundle["history"]["artifacts"]) == 1
    root = Path(__file__).resolve().parents[2]; output = tmp_path / "source-bundle.json"
    child = subprocess.run([sys.executable, str(root / "scripts/build_history_bundle.py"), "--sources-only",
        "--manifest", str(manifest), "--output", str(output)], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert json.loads(output.read_bytes()) == bundle
    # Built production endpoint with local storage bindings, not production proof.
    endpoint_check = r'''
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const {default: worker} = await import(pathToFileURL(process.argv[1]).href);
const bundle = JSON.parse(readFileSync(process.argv[2], "utf8"));
const objects = new Map(), statements = [];
const db = {prepare(sql) {return {values: [], bind(...values) {this.values=values; return this;},
  async run() {statements.push(sql); return {success:true};},
  async all() {statements.push(sql); return {results:[]};},
  async first() {statements.push(sql); return null;}};}};
const bucket = {async put(key,value) {objects.set(key,value); return {};},
  async head(key) {return objects.has(key) ? {customMetadata:{}} : null;},
  async get(key) {if (!objects.has(key)) return null; const value=objects.get(key);
    return {body:value, async text(){return Buffer.from(value).toString();},
      async json(){return JSON.parse(Buffer.from(value).toString());}};}};
const env = {DB:db, RAW_BUCKET:bucket, INGEST_API_KEY:"local-fixture",
  ASSETS:{fetch:async()=>new Response("not found",{status:404})}};
const ctx = {waitUntil(){},passThroughOnException(){}};
const response = await worker.fetch(new Request("http://localhost/api/v1/admin/history-import", {
  method:"POST", headers:{Authorization:"Bearer local-fixture","Content-Type":"application/json"},
  body:JSON.stringify(bundle)}),env,ctx);
const receipt = await response.json();
assert.equal(response.status,200,JSON.stringify(receipt));
assert.equal(receipt.status,"chunk_archived");
assert.equal(receipt.asOf,bundle.history.asOf);
assert.ok([...objects.keys()].some(key=>key.includes("/raw/hkex/")));
assert.ok([...objects.keys()].some(key=>key.includes("/normalized/hkex/")));
assert.ok(![...objects.keys()].some(key=>key.includes("/snapshots/") || key.includes("active")));
assert.ok(!statements.some(sql=>sql.includes("INSERT INTO daily_snapshot")));
'''
    endpoint = subprocess.run(["node", "--input-type=module", "-e", endpoint_check,
        str(root / "web/dist/server/index.js"), str(output)], cwd=root, capture_output=True, text=True)
    assert endpoint.returncode == 0, endpoint.stderr
    value["requested_dates"] = ["2026-02-30"]; manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        build_source_history_bundle_from_files(manifest_path=manifest, workspace_root=tmp_path)


def test_split_pcf_builds_have_stable_wire_identity_across_time(tmp_path, monkeypatch):
    from trading_desk.history_bundle import build_source_history_bundle_from_files
    test_history_bundle_compresses_wide_pcf_in_transit(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes()); manifest["requested_dates"] = ["2026-07-21"]
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr("time.time", lambda: 1000)
    checkpoint = build_source_history_bundle_from_files(manifest_path=manifest_path, workspace_root=tmp_path)
    monkeypatch.setattr("time.time", lambda: 2000)
    final = build_history_bundle_from_files(snapshot_path=tmp_path / "snapshot.json",
        manifest_path=manifest_path, workspace_root=tmp_path)
    assert checkpoint["history"]["artifacts"] == final["history"]["artifacts"]
    wire = base64.b64decode(final["history"]["artifacts"][0]["normalized"][0]["contentBase64"])
    assert wire[4:8] == b"\0\0\0\0"


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


def test_history_bundle_archives_future_only_scale_response_without_blocking_fallback(
    tmp_path: Path,
) -> None:
    """A later official response must not break an older complete snapshot."""

    artifacts: list[dict[str, object]] = []
    rows_by_artifact = [
        ("sse", "2026-08-22", [("2026-08-21", 100), ("2026-08-22", 130)]),
        ("szse", "2026-08-22", [("2026-08-21", 200), ("2026-08-22", 250)]),
        # This response belongs to a later source date and contains no rows
        # eligible for the fallback snapshot's as-of date.
        ("szse", "2026-08-23", [("2026-08-23", 260)]),
    ]
    for index, (source_id, requested_date, rows) in enumerate(rows_by_artifact):
        raw_path = tmp_path / f"data/raw/{source_id}/etf_scale/{requested_date}/{index}.json"
        metadata_path = raw_path.with_name(f"{index}.metadata.json")
        normalized_path = (
            tmp_path / f"data/normalized/{source_id}_etf_scale/{requested_date}/{index}.csv"
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        raw = f'{{"source":"{source_id}","date":"{requested_date}"}}'.encode()
        digest = hashlib.sha256(raw).hexdigest()
        raw_path.write_bytes(raw)
        metadata_path.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
        pd.DataFrame(
            [
                {
                    "trade_date": trade_date,
                    "market": "SSE" if source_id == "sse" else "SZSE",
                    "instrument_code": "510300" if source_id == "sse" else "159919",
                    "instrument_name": "测试ETF",
                    "shares": shares,
                }
                for trade_date, shares in rows
            ]
        ).to_csv(normalized_path, index=False)
        artifacts.append(
            {
                "source": {
                    "source_id": source_id,
                    "dataset": "etf_scale",
                    "url": f"https://{source_id}.example/etf-scale",
                    "date_semantics": "source_published",
                },
                "requested_date": requested_date,
                "observed_at": f"{requested_date}T18:00:00+08:00",
                "content_type": "application/json",
                "raw_path": str(raw_path.relative_to(tmp_path)),
                "metadata_path": str(metadata_path.relative_to(tmp_path)),
                "normalized_path": str(normalized_path.relative_to(tmp_path)),
                "sha256": digest,
            }
        )

    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"asOf": "2026-08-22"}), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "future-only-scale-test",
                "started_at": "2026-08-23T09:00:00+08:00",
                "finished_at": "2026-08-23T09:01:00+08:00",
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
    assert adjacent["asOf"] == "2026-08-22"
    assert len(adjacent["sourceReleaseIds"]) == 2
    assert any(
        item["requestedDate"] == "2026-08-23"
        for item in bundle["history"]["artifacts"]
    )
