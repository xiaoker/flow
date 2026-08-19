"""Replay a bounded pair of immutable R2 historical snapshots locally.

This is intentionally read-only.  It proves that a historical snapshot can be
downloaded, hash-checked, validated, and used to rebuild the compact ETF state
and market-history points without reading an active release or writing D1/R2.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from trading_desk.etf_flow_state import build_etf_flow_state
from trading_desk.read_models import validate_snapshot


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: Any) -> str:
    text = str(value or "").strip()
    date.fromisoformat(text)
    return text


def _point(series: Any, as_of: str) -> dict[str, Any] | None:
    if not isinstance(series, list):
        return None
    accepted = {as_of, as_of[5:]}
    for value in reversed(series):
        if isinstance(value, dict) and str(value.get("date") or "") in accepted:
            return value
    return None


def _market_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    as_of = _iso(snapshot.get("asOf"))
    margin = _point(snapshot.get("marginSeries"), as_of)
    connect = _point(snapshot.get("connectSeries"), as_of)
    return {
        "tradeDate": as_of,
        "marginAvailable": margin is not None,
        "marginBalance": (margin or {}).get("balance"),
        "connectAvailable": connect is not None,
        "southboundNet": (connect or {}).get("southboundNet"),
        "northboundTurnover": (connect or {}).get("northboundTurnover"),
    }


def _csv_summary(directory: Path, target: str, previous: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        dates = sorted(
            {
                str(row.get("trade_date") or "").strip()
                for row in rows
                if str(row.get("trade_date") or "").strip()
            }
        )
        files.append(
            {
                "file": path.name,
                "rows": len(rows),
                "dates": dates,
                "targetRows": sum(row.get("trade_date") == target for row in rows),
                "previousRows": sum(row.get("trade_date") == previous for row in rows),
            }
        )
    target_ready = any(item["targetRows"] > 0 for item in files)
    pcf_ready = any("pcf" in item["file"] and item["targetRows"] > 0 for item in files)
    return {
        "files": files,
        "targetReady": target_ready,
        "pcfReady": pcf_ready,
        "complete": target_ready and pcf_ready,
    }


def replay(
    *,
    snapshot_dir: Path,
    target_date: str,
    normalized_dir: Path | None = None,
    normalized_report: Path | None = None,
    selection_path: Path | None = None,
) -> dict[str, Any]:
    target_date = _iso(target_date)
    paths = sorted(snapshot_dir.glob("*.json"))
    if not paths:
        raise ValueError("no R2 snapshots were restored")

    snapshots: list[tuple[Path, dict[str, Any]]] = []
    snapshot_checks: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"snapshot is not an object: {path.name}")
        as_of = _iso(payload.get("asOf"))
        validate_snapshot(payload, require_materialized=False)
        digest = _sha256(path)
        snapshots.append((path, payload))
        snapshot_checks.append(
            {
                "file": path.name,
                "asOf": as_of,
                "sha256": digest,
                "bytes": path.stat().st_size,
                "schemaValid": True,
            }
        )
    if target_date not in {str(payload.get("asOf")) for _, payload in snapshots}:
        raise ValueError(f"target snapshot was not restored: {target_date}")

    state = build_etf_flow_state(snapshots, max_trading_days=260)
    if target_date not in state["dates"]:
        raise ValueError(f"ETF state replay omitted target date: {target_date}")
    market_rows = [_market_row(payload) for _, payload in snapshots]
    normalized = None
    if normalized_dir is not None:
        normalized = _csv_summary(
            normalized_dir,
            target_date,
            state["dates"][-2] if len(state["dates"]) > 1 else target_date,
        )
    if normalized_report is not None and normalized_report.exists():
        normalized_report_value = json.loads(normalized_report.read_text(encoding="utf-8"))
    else:
        normalized_report_value = None

    expected_digests: dict[str, str] = {}
    if selection_path is not None and selection_path.exists():
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        for key in ("target", "previous"):
            item = selection.get(key) if isinstance(selection, dict) else None
            if isinstance(item, dict):
                expected_digests[str(item.get("as_of"))] = str(item.get("snapshot_sha256"))
    hash_matches = all(
        not expected_digests
        or expected_digests.get(item["asOf"]) in {None, item["sha256"]}
        for item in snapshot_checks
    )
    normalized_complete = bool(normalized is None or normalized.get("complete"))
    return {
        "schemaVersion": 1,
        "status": "pass" if hash_matches and normalized_complete else "conditional",
        "readOnly": True,
        "targetDate": target_date,
        "snapshotCount": len(snapshots),
        "snapshotChecks": snapshot_checks,
        "checks": {
            "rawSha256": hash_matches,
            "snapshotSchema": True,
            "etfStateReplay": True,
            "marketHistoryReplay": all(row["marginAvailable"] or row["connectAvailable"] for row in market_rows),
            "normalizedArtifacts": normalized_complete,
        },
        "etfState": {
            "dates": state["dates"],
            "asOf": state["dates"][-1],
            "instrumentCount": state["quality"]["instrumentCount"],
            "populatedValues": state["quality"]["populatedValues"],
            "quality": state["quality"]["status"],
        },
        "marketHistory": market_rows,
        "normalized": normalized,
        "normalizedFetchReport": normalized_report_value,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay one immutable R2 historical pair")
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--normalized-dir", type=Path)
    parser.add_argument("--normalized-report", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(
        snapshot_dir=args.snapshot_dir,
        target_date=args.target_date,
        normalized_dir=args.normalized_dir,
        normalized_report=args.normalized_report,
        selection_path=args.selection,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
