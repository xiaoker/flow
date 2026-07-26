from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .etf_classification import CLASSIFICATION_VERSION, classify_etf


STATE_VERSION = 1
MAX_TRADING_DAYS = 260
CALCULATION_METHOD = "sum_of_daily_official_share_changes"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classification(item: dict[str, Any]) -> Any:
    asset_class = str(item.get("assetClass") or "")
    return classify_etf(
        name=str(item.get("name") or item.get("code") or ""),
        code=str(item.get("code") or "").zfill(6),
        market=str(item.get("market") or "SSE"),
        etf_type="跨境" if asset_class == "跨境" else None,
        investment_type=asset_class or None,
    )


def build_etf_flow_state(
    snapshots: Iterable[tuple[Path, dict[str, Any]]],
    *,
    max_trading_days: int = MAX_TRADING_DAYS,
) -> dict[str, Any]:
    """Build a compact rolling ETF daily-flow state from verified snapshots.

    The state contains derived daily share changes only. Official raw responses
    and normalized CSVs remain the immutable source of truth in R2.
    """

    by_date: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, snapshot in snapshots:
        as_of = str(snapshot.get("asOf") or "")
        if not as_of:
            raise ValueError(f"Snapshot did not contain asOf: {path}")
        if as_of in by_date:
            raise ValueError(f"Duplicate snapshot date: {as_of}")
        by_date[as_of] = (path, snapshot)
    if not by_date:
        raise ValueError("No ETF snapshots were provided")

    dates = sorted(by_date)[-max_trading_days:]
    date_index = {trade_date: index for index, trade_date in enumerate(dates)}
    instruments: dict[str, dict[str, Any]] = {}
    source_snapshots: list[dict[str, str]] = []
    rejected_rows = 0
    duplicate_rows = 0

    for trade_date in dates:
        path, snapshot = by_date[trade_date]
        source_snapshots.append(
            {"date": trade_date, "sha256": _sha256(path), "file": path.name}
        )
        terminal = snapshot.get("etfTerminal") or {}
        seen: set[str] = set()
        for raw_item in terminal.get("instruments") or []:
            if not isinstance(raw_item, dict):
                rejected_rows += 1
                continue
            code = str(raw_item.get("code") or "").zfill(6)
            market = str(raw_item.get("market") or "SSE")
            key = f"{market}:{code}"
            if len(code) != 6 or not code.isdigit() or market not in {"SSE", "SZSE"}:
                rejected_rows += 1
                continue
            if key in seen:
                duplicate_rows += 1
                continue
            seen.add(key)
            value = (raw_item.get("periods") or {}).get("1d")
            if value is None:
                continue
            try:
                daily_value = float(value)
            except (TypeError, ValueError):
                rejected_rows += 1
                continue
            if not math.isfinite(daily_value):
                rejected_rows += 1
                continue

            classification = _classification(raw_item)
            if not classification.eligible:
                continue
            row = instruments.setdefault(
                key,
                {
                    "code": code,
                    "name": str(raw_item.get("name") or code),
                    "market": market,
                    "category": classification.category,
                    "groupType": classification.group_type,
                    "assetClass": classification.asset_class,
                    "classificationSource": classification.source,
                    "classificationConfidence": classification.confidence,
                    "classificationRule": classification.rule_id,
                    "includeInGroup": classification.include_in_group,
                    "values": [None] * len(dates),
                },
            )
            row["name"] = str(raw_item.get("name") or row["name"])
            row["category"] = classification.category
            row["groupType"] = classification.group_type
            row["assetClass"] = classification.asset_class
            row["classificationSource"] = classification.source
            row["classificationConfidence"] = classification.confidence
            row["classificationRule"] = classification.rule_id
            row["includeInGroup"] = classification.include_in_group
            row["values"][date_index[trade_date]] = round(daily_value, 6)

    ordered = sorted(
        (
            row
            for row in instruments.values()
            if any(value is not None for value in row["values"])
        ),
        key=lambda row: (row["market"], row["code"]),
    )
    populated_values = sum(
        sum(value is not None for value in row["values"]) for row in ordered
    )
    generated_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
        timespec="seconds"
    )
    return {
        "version": STATE_VERSION,
        "classificationVersion": CLASSIFICATION_VERSION,
        "calculationMethod": CALCULATION_METHOD,
        "generatedAt": generated_at,
        "dates": dates,
        "instruments": ordered,
        "sourceSnapshots": source_snapshots,
        "quality": {
            "snapshotCount": len(dates),
            "instrumentCount": len(ordered),
            "populatedValues": populated_values,
            "rejectedRows": rejected_rows,
            "duplicateRows": duplicate_rows,
            "status": (
                "pass"
                if rejected_rows == 0 and duplicate_rows == 0
                else "conditional_pass"
            ),
        },
    }


def build_etf_flow_state_from_directory(
    directory: Path, *, max_trading_days: int = MAX_TRADING_DAYS
) -> dict[str, Any]:
    snapshots: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Expected JSON object: {path}")
        snapshots.append((path, payload))
    return build_etf_flow_state(
        snapshots,
        max_trading_days=max_trading_days,
    )
