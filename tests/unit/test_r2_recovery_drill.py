from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.r2_recovery_drill import replay
from scripts.select_r2_recovery_pair import select


def _snapshot(path: Path, as_of: str, value: float) -> str:
    payload = {
        "asOf": as_of,
        "quality": {"pass": 10, "warn": 0, "fail": 0},
        "etfTerminal": {
            "instruments": [
                {
                    "code": "510300",
                    "name": "沪深300ETF",
                    "market": "SSE",
                    "assetClass": "股票",
                    "periods": {"1d": value},
                }
            ],
            "dailySeries": [],
            "groupPeriods": [],
        },
        "marginSeries": [{"date": as_of, "balance": 100.0}],
        "connectSeries": [{"date": as_of, "southboundNet": 2.0}],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_selects_explicit_target_and_immediate_predecessor() -> None:
    payload = [
        {
            "results": [
                {"as_of": "2026-08-10", "r2_object_key": "a", "snapshot_sha256": "a" * 64},
                {"as_of": "2026-08-11", "r2_object_key": "b", "snapshot_sha256": "b" * 64},
                {"as_of": "2026-08-12", "r2_object_key": "c", "snapshot_sha256": "c" * 64},
            ]
        }
    ]

    result = select(payload, target_date="2026-08-12")

    assert result["selection"] == "explicit"
    assert result["target"]["as_of"] == "2026-08-12"
    assert result["previous"]["as_of"] == "2026-08-11"
    assert result["candidateCount"] == 3


def test_replays_hash_checked_snapshots_into_state_and_market_history(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    previous = snapshot_dir / "2026-08-10.json"
    target = snapshot_dir / "2026-08-11.json"
    previous_sha = _snapshot(previous, "2026-08-10", 1.0)
    target_sha = _snapshot(target, "2026-08-11", -0.5)
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "target": {"as_of": "2026-08-11", "snapshot_sha256": target_sha},
                "previous": {"as_of": "2026-08-10", "snapshot_sha256": previous_sha},
            }
        ),
        encoding="utf-8",
    )
    normalized_dir = tmp_path / "normalized"
    normalized_dir.mkdir()
    (normalized_dir / "shares.csv").write_text(
        "trade_date,shares\n2026-08-10,100\n2026-08-11,101\n",
        encoding="utf-8",
    )
    (normalized_dir / "pcf.csv").write_text(
        "trade_date,code\n2026-08-10,510300\n2026-08-11,510300\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "normalized-report.json"
    report_path.write_text(json.dumps({"availableDates": ["2026-08-10", "2026-08-11"]}), encoding="utf-8")

    result = replay(
        snapshot_dir=snapshot_dir,
        target_date="2026-08-11",
        normalized_dir=normalized_dir,
        normalized_report=report_path,
        selection_path=selection_path,
    )

    assert result["status"] == "pass"
    assert result["readOnly"] is True
    assert result["checks"] == {
        "rawSha256": True,
        "snapshotSchema": True,
        "etfStateReplay": True,
        "marketHistoryReplay": True,
        "normalizedArtifacts": True,
    }
    assert result["etfState"]["dates"] == ["2026-08-10", "2026-08-11"]
    assert result["etfState"]["instrumentCount"] == 1
    assert result["marketHistory"][1]["tradeDate"] == "2026-08-11"
