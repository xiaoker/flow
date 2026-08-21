"""Legacy/v2 PCF shadow parity contracts and budget evidence.

PR4 deliberately keeps this module offline with respect to production.  The
caller may fetch both adapters, but this module only compares their normalized
frames and turns the result into a bounded, auditable report.  It never writes
R2/D1 and it never selects a production read model.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

PARITY_SCHEMA_VERSION = 1
DIFFERENCE_CATEGORIES = (
    "DATE_SEMANTICS_CHANGE",
    "LEGACY_ZERO_QUANTITY_OMISSION",
    "REAL_DATA_ANOMALY",
)

_CORE_FIELDS = (
    "quantity",
    "creation_unit",
    "substitution_flag",
    "stock_code",
    "listing_market",
)
_DATE_FIELDS = ("pcf_date", "pcf_previous_date", "date_basis")


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _code(value: Any, width: int = 6) -> str:
    value = _text(value)
    return value.zfill(width) if value.isdigit() else value


def _number(value: Any) -> int | float | str:
    value = _text(value)
    if not value:
        return ""
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _normalise_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    return {
        "etf_code": _code(row.get("etf_code", row.get("instrument_code"))),
        "etf_name": _text(row.get("etf_name", row.get("instrument_name"))),
        "stock_code": _text(row.get("stock_code")),
        "stock_name": _text(row.get("stock_name")),
        "listing_market": _text(row.get("listing_market")),
        "quantity": _number(row.get("quantity")),
        "creation_unit": _number(row.get("creation_unit")),
        "substitution_flag": _text(row.get("substitution_flag")),
        "pcf_date": _text(row.get("pcf_date")),
        "pcf_previous_date": _text(row.get("pcf_previous_date")),
        "date_basis": _text(row.get("date_basis")),
    }


def _frame_map(frame: pd.DataFrame | None) -> tuple[dict[tuple[str, str, str], dict[str, Any]], int]:
    if frame is None or frame.empty:
        return {}, 0
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicates = 0
    for raw in frame.to_dict(orient="records"):
        row = _normalise_row(raw)
        key = (row["etf_code"], row["stock_code"], row["listing_market"])
        if not key[0] or not key[1]:
            continue
        if key in result:
            duplicates += 1
        # The adapter is expected to be deterministic.  Keep the last row so
        # a duplicate is still visible in the duplicate counter rather than
        # silently multiplying a difference.
        result[key] = row
    return result, duplicates


def _same(left: Any, right: Any) -> bool:
    return _text(left) == _text(right) if isinstance(left, str) or isinstance(right, str) else left == right


def _group_summary(details: Iterable[Mapping[str, Any]], key_name: str) -> list[dict[str, Any]]:
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for detail in details:
        key = str(detail.get(key_name) or "")
        groups[key][str(detail["category"])] += 1
    return [
        {
            key_name: key,
            "differences": sum(counter.values()),
            "byCategory": dict(sorted(counter.items())),
        }
        for key, counter in sorted(groups.items())
    ]


def compare_pcf_frames(
    legacy: pd.DataFrame | None,
    v2: pd.DataFrame | None,
    *,
    max_detail_rows: int = 20_000,
) -> dict[str, Any]:
    """Compare normalized legacy/v2 rows without treating expected date shifts as errors."""

    if max_detail_rows <= 0:
        raise ValueError("max_detail_rows must be positive")
    legacy_map, legacy_duplicates = _frame_map(legacy)
    v2_map, v2_duplicates = _frame_map(v2)
    details: list[dict[str, Any]] = []
    total_differences = 0
    for key in sorted(set(legacy_map) | set(v2_map)):
        old = legacy_map.get(key)
        new = v2_map.get(key)
        category: str | None = None
        if old is not None and new is not None:
            core_equal = all(_same(old.get(field), new.get(field)) for field in _CORE_FIELDS)
            date_equal = all(_same(old.get(field), new.get(field)) for field in _DATE_FIELDS)
            if core_equal and date_equal:
                continue
            category = "DATE_SEMANTICS_CHANGE" if core_equal else "REAL_DATA_ANOMALY"
        elif new is not None and _number(new.get("quantity")) == 0:
            category = "LEGACY_ZERO_QUANTITY_OMISSION"
        else:
            category = "REAL_DATA_ANOMALY"

        total_differences += 1
        if len(details) < max_detail_rows:
            details.append(
                {
                    "category": category,
                    "etfCode": key[0],
                    "stockCode": key[1],
                    "listingMarket": key[2],
                    "legacy": old,
                    "v2": new,
                }
            )

    counts = Counter(detail["category"] for detail in details)
    # If the detail cap is reached, counts below are deliberately corrected
    # from the full key iteration so a report cannot call a truncated result a
    # clean parity run.
    all_counts: Counter[str] = Counter()
    for key in sorted(set(legacy_map) | set(v2_map)):
        old = legacy_map.get(key)
        new = v2_map.get(key)
        if old is not None and new is not None:
            core_equal = all(_same(old.get(field), new.get(field)) for field in _CORE_FIELDS)
            date_equal = all(_same(old.get(field), new.get(field)) for field in _DATE_FIELDS)
            if core_equal and date_equal:
                continue
            all_counts["DATE_SEMANTICS_CHANGE" if core_equal else "REAL_DATA_ANOMALY"] += 1
        elif new is not None and _number(new.get("quantity")) == 0:
            all_counts["LEGACY_ZERO_QUANTITY_OMISSION"] += 1
        else:
            all_counts["REAL_DATA_ANOMALY"] += 1

    etf_details = [
        {**detail, "etfCode": detail["etfCode"]}
        for detail in details
    ]
    stock_details = [
        {**detail, "stockCode": detail["stockCode"]}
        for detail in details
    ]
    anomaly_count = all_counts["REAL_DATA_ANOMALY"]
    return {
        "legacyRows": len(legacy_map),
        "v2Rows": len(v2_map),
        "differenceRows": total_differences,
        "differenceRowsReported": len(details),
        "detailTruncated": total_differences > len(details),
        "duplicateKeyCount": legacy_duplicates + v2_duplicates,
        "byCategory": dict(sorted(all_counts.items())),
        "unexpectedAnomalyCount": anomaly_count,
        "etf": _group_summary(etf_details, "etfCode"),
        "stock": _group_summary(stock_details, "stockCode"),
        "market": {
            "legacyRows": len(legacy_map),
            "v2Rows": len(v2_map),
            "differenceRows": total_differences,
            "byCategory": dict(sorted(all_counts.items())),
        },
        "details": details,
        "detailCategoryCounts": dict(sorted(counts.items())),
    }


def _load_json_payload(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    candidate = Path(path)
    if not candidate.exists():
        return {}
    content = candidate.read_bytes()
    if content[:2] == b"\x1f\x8b" or candidate.suffix in {".gz", ".gzip"}:
        content = gzip.decompress(content)
    payload = json.loads(content.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _artifact_metrics(artifact: Any) -> dict[str, Any]:
    if artifact is None:
        return {"requests": 0, "statusCounts": {}, "rateLimitCount": 0, "objects": 0, "bytes": 0}
    payload = _load_json_payload(getattr(artifact, "raw_path", None))
    requests = payload.get("requests") if isinstance(payload.get("requests"), list) else []
    statuses: Counter[str] = Counter()
    rate_limits = 0
    for item in requests:
        if not isinstance(item, dict):
            continue
        status_values = [
            item.get(field)
            for field in ("status", "basic_status", "component_status")
            if item.get(field) is not None
        ]
        for value in status_values:
            normalized = str(value)
            statuses[normalized] += 1
            if normalized in {"403", "429"}:
                rate_limits += 1
    paths = [getattr(artifact, "raw_path", None), getattr(artifact, "metadata_path", None)]
    normalized = str(getattr(artifact, "normalized_path", "") or "")
    paths.extend(normalized.split(";") if normalized else [])
    for item in payload.get("raw_xml", []) if isinstance(payload.get("raw_xml"), list) else []:
        if isinstance(item, dict):
            paths.extend([item.get("raw_path"), item.get("metadata_path")])
    existing = [Path(path) for path in paths if path and Path(path).exists()]
    return {
        # Legacy JSON emits one log row per ETF but performs basic + component
        # HTTP requests; v2 emits one XML request row per ETF.
        "requests": sum(
            2 if isinstance(item, dict) and ("basic_status" in item or "component_status" in item) else 1
            for item in requests
        ),
        "statusCounts": dict(sorted(statuses.items())),
        "rateLimitCount": rate_limits,
        "objects": len(existing),
        "bytes": sum(path.stat().st_size for path in existing),
        "acceptedEtfCount": (
            sum(1 for item in requests if isinstance(item, dict) and item.get("accepted") is True)
            if any(isinstance(item, dict) and "accepted" in item for item in requests)
            else int(payload.get("captured_etf_count", 0))
        ),
        "payload": payload,
    }


def build_pcf_parity_report(
    *,
    plan: Mapping[str, Any],
    trade_date: str,
    previous_date: str,
    legacy_frame: pd.DataFrame | None = None,
    v2_frame: pd.DataFrame | None = None,
    legacy_artifact: Any = None,
    v2_artifact: Any = None,
    elapsed_ms: int = 0,
    run_id: str = "",
    max_detail_rows: int = 20_000,
) -> dict[str, Any]:
    legacy_metrics = _artifact_metrics(legacy_artifact)
    v2_metrics = _artifact_metrics(v2_artifact)
    comparison = compare_pcf_frames(legacy_frame, v2_frame, max_detail_rows=max_detail_rows)
    required = int((plan.get("counts") or {}).get("required", 0))
    v2_accepted = int(v2_metrics.get("acceptedEtfCount", 0))
    legacy_accepted = int(legacy_metrics.get("acceptedEtfCount", 0))
    rate_limits = int(legacy_metrics["rateLimitCount"]) + int(v2_metrics["rateLimitCount"])
    full_coverage = required == v2_accepted and required == legacy_accepted
    blocked = str(plan.get("status")) in {"BLOCKED_UNIVERSE_ANOMALY", "BLOCKED_QUALITY"}
    if blocked:
        status = str(plan.get("status"))
    elif required == 0:
        status = "READY_NO_CHANGE"
    elif rate_limits or comparison["unexpectedAnomalyCount"] or comparison["detailTruncated"]:
        status = "FAIL"
    elif not full_coverage:
        status = "PENDING"
    else:
        status = "PASS"

    all_warnings = [
        str(item)
        for artifact in (legacy_artifact, v2_artifact)
        for item in (getattr(artifact, "warnings", []) or [])
    ]
    return {
        "schemaVersion": PARITY_SCHEMA_VERSION,
        "status": status,
        "shadowOnly": True,
        "runId": run_id,
        "tradeDate": trade_date,
        "previousDate": previous_date,
        "plan": {
            "status": plan.get("status"),
            "planSha256": plan.get("planSha256"),
            "counts": plan.get("counts", {}),
            "requiredCodes": [
                item.get("etfCode") for item in plan.get("items", []) if item.get("required")
            ],
        },
        "coverage": {
            "required": required,
            "legacyAccepted": legacy_accepted,
            "v2Accepted": v2_accepted,
            "legacyPpm": int(legacy_accepted * 1_000_000 / required) if required else 1_000_000,
            "v2Ppm": int(v2_accepted * 1_000_000 / required) if required else 1_000_000,
            "complete": full_coverage,
        },
        "comparison": comparison,
        "warnings": list(dict.fromkeys(all_warnings)),
        "budget": {
            "elapsedMs": max(0, int(elapsed_ms)),
            "estimatedActionsMinutes": round(max(0, elapsed_ms) / 60_000, 3),
            "legacyRequests": legacy_metrics["requests"],
            "v2Requests": v2_metrics["requests"],
            "totalRequests": legacy_metrics["requests"] + v2_metrics["requests"],
            "httpStatusCounts": {
                "legacy": legacy_metrics["statusCounts"],
                "v2": v2_metrics["statusCounts"],
            },
            "http403429Count": rate_limits,
            "localObjectCount": legacy_metrics["objects"] + v2_metrics["objects"],
            "localBytes": legacy_metrics["bytes"] + v2_metrics["bytes"],
            "r2ObjectsWritten": 0,
            "r2BytesWritten": 0,
            "d1Writes": 0,
            "productionPublishCalls": 0,
        },
        "budgetGuard": {
            "r2WithinShadowGuard": True,
            "d1WithinShadowGuard": True,
            "productionReadPathChanged": False,
            "productionPublishPathCalled": False,
        },
    }


def summarize_pcf_parity_reports(
    reports: Iterable[Mapping[str, Any]], *, required_days: int = 5
) -> dict[str, Any]:
    """Build the PR4 five-trading-day gate without pretending missing days pass."""

    if required_days <= 0:
        raise ValueError("required_days must be positive")
    by_date: dict[str, Mapping[str, Any]] = {}
    for report in reports:
        date = _text(report.get("tradeDate"))
        if date:
            by_date[date] = report
    days = [by_date[key] for key in sorted(by_date)]
    passing = [
        report
        for report in days
        if report.get("status") in {"PASS", "READY_NO_CHANGE"}
        and bool((report.get("coverage") or {}).get("complete", False))
        and int((report.get("comparison") or {}).get("unexpectedAnomalyCount", 0)) == 0
        and int((report.get("budget") or {}).get("http403429Count", 0)) == 0
    ]
    budget_rows = [report.get("budget") or {} for report in days]
    totals = {
        "requests": sum(int(row.get("totalRequests", 0)) for row in budget_rows),
        "localBytes": sum(int(row.get("localBytes", 0)) for row in budget_rows),
        "r2BytesWritten": sum(int(row.get("r2BytesWritten", 0)) for row in budget_rows),
        "d1Writes": sum(int(row.get("d1Writes", 0)) for row in budget_rows),
        "estimatedActionsMinutes": round(sum(float(row.get("estimatedActionsMinutes", 0)) for row in budget_rows), 3),
    }
    return {
        "schemaVersion": PARITY_SCHEMA_VERSION,
        "status": "PASS" if len(days) >= required_days and len(passing) == len(days) else "INSUFFICIENT_OBSERVATIONS" if len(days) < required_days else "FAIL",
        "requiredDays": required_days,
        "observedDays": len(days),
        "passingDays": len(passing),
        "dates": sorted(by_date),
        "missingDays": max(0, required_days - len(days)),
        "failedDates": [report.get("tradeDate") for report in days if report not in passing],
        "totals": totals,
        "guardrails": {
            "r2BytesWritten": {"value": totals["r2BytesWritten"], "limit": 0, "within": totals["r2BytesWritten"] == 0},
            "d1Writes": {"value": totals["d1Writes"], "limit": 0, "within": totals["d1Writes"] == 0},
            "productionPublishCalls": {"value": 0, "limit": 0, "within": True},
        },
        "observations": [
            {
                "tradeDate": report.get("tradeDate"),
                "status": report.get("status"),
                "coverage": report.get("coverage"),
                "unexpectedAnomalyCount": (report.get("comparison") or {}).get("unexpectedAnomalyCount", 0),
                "http403429Count": (report.get("budget") or {}).get("http403429Count", 0),
            }
            for report in days
        ],
    }
