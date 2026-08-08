"""Patch/rebuild the compact ETF state from verified recent D1 raw rows.

The rolling state already contains the older 260-day window.  This utility
replaces only the recent interval whose SZSE release was missing or assigned
to the wrong date, calculating each daily value from adjacent official share
snapshots.  It never fills a gap with a multi-day delta.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_desk.etf_classification import CLASSIFICATION_VERSION, classify_etf


MAX_DAYS = 260


def _classification(row: dict[str, Any]):
    return classify_etf(
        name=str(row.get("instrument_name") or row.get("instrument_code") or ""),
        code=str(row.get("instrument_code") or "").zfill(6),
        market=str(row.get("market") or ""),
        etf_type="ETF" if str(row.get("market")) == "SZSE" else None,
        investment_type=str(row.get("category") or ""),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--recovery-manifest", type=Path, required=True)
    parser.add_argument("--allowed-codes", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8"))
    rows = json.loads(args.rows.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("recent row export is empty")
    allowed_codes: set[str] | None = None
    if args.allowed_codes:
        allowed_codes = {
            str(value).strip().zfill(6)
            for value in json.loads(args.allowed_codes.read_text(encoding="utf-8"))
            if str(value).strip().isdigit()
        }
    recent_dates = sorted({str(row["trade_date"]) for row in rows})
    if len(recent_dates) < 2:
        raise ValueError("at least two adjacent official dates are required")
    by_date_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if (
            allowed_codes is not None
            and str(row.get("market")) == "SZSE"
            and str(row["instrument_code"]).zfill(6) not in allowed_codes
        ):
            continue
        key = (str(row["trade_date"]), f"{row['market']}:{str(row['instrument_code']).zfill(6)}")
        if key in by_date_key:
            raise ValueError(f"duplicate ranked row: {key}")
        by_date_key[key] = row

    old_dates = [str(value) for value in state.get("dates") or []]
    dates = sorted(set(old_dates) | set(recent_dates))[-MAX_DAYS:]
    index = {value: position for position, value in enumerate(dates)}
    recent_manifest = json.loads(args.recovery_manifest.read_text(encoding="utf-8"))
    raw_hashes = {str(item["requested_date"]): str(item["sha256"]) for item in recent_manifest["artifacts"]}
    instruments: dict[str, dict[str, Any]] = {}
    for original in state.get("instruments") or []:
        if not isinstance(original, dict):
            continue
        code = str(original.get("code") or "").zfill(6)
        market = str(original.get("market") or "SSE")
        key = f"{market}:{code}"
        values_by_date = {
            old_date: value
            for old_date, value in zip(old_dates, original.get("values") or [], strict=False)
        }
        copied = {item_key: original.get(item_key) for item_key in (
            "code", "name", "market", "category", "groupType", "assetClass",
            "classificationSource", "classificationConfidence", "classificationRule",
            "includeInGroup",
        )}
        copied["code"] = code
        copied["market"] = market
        copied["values"] = [values_by_date.get(date) for date in dates]
        instruments[key] = copied

    # Add newly listed ETFs and refresh identity/classification from the
    # exchange rows.  The raw category is an official field; industry grouping
    # remains the same audited classifier used by the existing state.
    for row in rows:
        if (
            allowed_codes is not None
            and str(row.get("market")) == "SZSE"
            and str(row["instrument_code"]).zfill(6) not in allowed_codes
        ):
            continue
        key = f"{row['market']}:{str(row['instrument_code']).zfill(6)}"
        classification = _classification(row)
        if not classification.eligible:
            continue
        item = instruments.setdefault(
            key,
            {
                "code": str(row["instrument_code"]).zfill(6),
                "name": str(row.get("instrument_name") or row["instrument_code"]),
                "market": str(row["market"]),
                "values": [None] * len(dates),
            },
        )
        item.update(
            {
                "name": str(row.get("instrument_name") or item.get("name") or row["instrument_code"]),
                "category": classification.category,
                "groupType": classification.group_type,
                "assetClass": classification.asset_class,
                "classificationSource": classification.source,
                "classificationConfidence": classification.confidence,
                "classificationRule": classification.rule_id,
                "includeInGroup": classification.include_in_group,
            }
        )

    # A daily change is valid only when both adjacent official rows exist.
    # This prevents 7/30 -> 8/06 from being mislabelled as a one-day flow.
    for trade_date in recent_dates:
        previous = max((value for value in dates if value < trade_date), default=None)
        # The export intentionally starts at the last already-published date;
        # do not overwrite that date's existing state value just because its
        # older predecessor was not part of this repair window.
        if previous not in recent_dates:
            continue
        for key, item in instruments.items():
            current = by_date_key.get((trade_date, key))
            prior = by_date_key.get((previous, key)) if previous else None
            value: float | None = None
            if current is not None and prior is not None:
                value = round((int(current["shares"]) - int(prior["shares"])) / 100_000_000, 6)
            item["values"][index[trade_date]] = value

    ordered = sorted(
        (item for item in instruments.values() if any(value is not None for value in item["values"])),
        key=lambda item: (item["market"], item["code"]),
    )
    source_by_date = {str(item["date"]): item for item in state.get("sourceSnapshots") or [] if isinstance(item, dict)}
    for date in dates:
        if date in raw_hashes:
            # Include the raw release hash and the exact D1 release interval in
            # the state provenance.  It is intentionally not a dashboard hash.
            source_by_date[date] = {
                "date": date,
                "sha256": raw_hashes[date],
                "file": f"d1-etf-daily:{raw_hashes[date][:16]}",
            }
    source_snapshots = [source_by_date[date] for date in dates if date in source_by_date]
    populated = sum(sum(value is not None for value in item["values"]) for item in ordered)
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    rebuilt = {
        "version": int(state.get("version") or 1),
        "classificationVersion": CLASSIFICATION_VERSION,
        "calculationMethod": state.get("calculationMethod", "sum_of_daily_official_share_changes"),
        "cashCalculationMethod": state.get("cashCalculationMethod", "sum_of_daily_share_changes_times_same_day_nav"),
        "generatedAt": generated_at,
        "dates": dates,
        "instruments": ordered,
        "sourceSnapshots": source_snapshots,
        "quality": {
            "snapshotCount": len(dates),
            "instrumentCount": len(ordered),
            "populatedValues": populated,
            "rejectedRows": 0,
            "duplicateRows": 0,
            "status": "pass" if len(source_snapshots) == len(dates) else "conditional_pass",
            "rebuild": "d1_etf_daily_recent_raw",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rebuilt, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(json.dumps({
        "from": dates[0], "to": dates[-1], "dateCount": len(dates),
        "instrumentCount": len(ordered), "populatedValues": populated,
        "sourceSnapshots": len(source_snapshots), "sha256": digest,
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
