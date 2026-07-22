from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from trading_desk.history_bundle import (
    HISTORY_RETENTION_DAYS,
    build_history_bundle_from_files,
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
