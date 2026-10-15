"""Bounded, offline dependency planner for ETF daily and period models.

The planner consumes explicit official-session dates plus immutable raw and
normalized evidence descriptors. It does not infer a calendar from observed
snapshots, fetch data, access D1/R2, build artifacts, or activate releases.
Its output is a minimal dependency plan: source gaps are listed by date/market/
stage, and only missing or stale downstream model nodes are marked for build.
"""

from __future__ import annotations

import hashlib
import base64
import json
import re
from datetime import date
from typing import Any, Iterable

from .etf_classification import CLASSIFICATION_VERSION
from .etf_flow_state import CALCULATION_METHOD, CASH_CALCULATION_METHOD


PLAN_CONTRACT_VERSION = "flow-etf-period-dependencies-v0.7.0"
OFFICIAL_CALENDAR_SOURCE = "sse_official_trading_calendar"
OFFICIAL_MARGIN_DATES_SOURCE = "sse_official_margin_business_dates"
MAX_TARGET_DATES = 280
MAX_CALENDAR_DATES = 533  # 280 target sessions + 252 lookback sessions + baseline.
MAX_SOURCE_EVIDENCE = MAX_CALENDAR_DATES * 2
MAX_PERIODS = ("1d", "3d", "1w", "2w", "1m", "3m", "6m", "ytd", "12m")
PERIOD_LOOKBACKS = {
    "1d": 1,
    "3d": 3,
    "1w": 5,
    "2w": 10,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "12m": 252,
}
PERIOD_METRICS = ("shares", "estimatedAmount")
SAMPLE_SCOPES = ("market", "category")
MAX_EXISTING_MODELS = MAX_TARGET_DATES * (
    len(MAX_PERIODS) * len(PERIOD_METRICS) * len(SAMPLE_SCOPES)
    + len(PERIOD_METRICS) * len(SAMPLE_SCOPES)
)
MARKETS = ("SSE", "SZSE")
FORMULA_VERSIONS = {
    "shares": CALCULATION_METHOD,
    "estimatedAmount": CASH_CALCULATION_METHOD,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGE_STATUS = {"AVAILABLE", "MISSING", "UNKNOWN"}


def _date(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid:{value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field}_invalid:{value}")
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field}_invalid")
    return value


def _count(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field}_invalid")
    return value


def _bounded_values(values: Iterable[Any], *, limit: int, field: str) -> list[Any]:
    """Consume at most limit + 1 values so unbounded iterators fail closed."""
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{field}_invalid") from exc
    collected: list[Any] = []
    for _ in range(limit + 1):
        try:
            collected.append(next(iterator))
        except StopIteration:
            return collected
    raise ValueError(f"{field}_limit_exceeded")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _calendar(value: Any) -> tuple[list[str], str]:
    if not isinstance(value, dict):
        raise ValueError("official_calendar_missing")
    if not isinstance(value.get("sourceId"),str) or value.get("sourceId") not in {OFFICIAL_CALENDAR_SOURCE, OFFICIAL_MARGIN_DATES_SOURCE}:
        raise ValueError("official_calendar_source_invalid")
    source_hash = _sha256(value.get("sha256"), field="official_calendar_sha256")
    raw_dates = value.get("dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise ValueError("official_calendar_dates_missing")
    if len(raw_dates) > MAX_CALENDAR_DATES:
        raise ValueError("official_calendar_limit_exceeded")
    dates = [_date(item, field="official_calendar_date") for item in raw_dates]
    if dates != sorted(set(dates)):
        raise ValueError("official_calendar_dates_not_strictly_increasing")
    expected_dates_hash = _canonical_hash(dates)
    if value.get("datesSha256") != expected_dates_hash:
        raise ValueError("official_calendar_dates_hash_mismatch")
    if value.get("sourceId") == OFFICIAL_MARGIN_DATES_SOURCE:
        evidence = value.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"contentBase64", "rangeStart", "rangeEnd"}:
            raise ValueError("official_business_dates_evidence_missing")
        start = _date(evidence["rangeStart"], field="official_calendar_range")
        end = _date(evidence["rangeEnd"], field="official_calendar_range")
        encoded = evidence["contentBase64"]
        if not isinstance(encoded, str) or len(encoded) > 2_800_000:
            raise ValueError("official_business_dates_size_exceeded")
        try:
            raw = base64.b64decode(encoded, validate=True)
            response = json.loads(raw)
            rows = response["result"]
            page = response["pageHelp"]
            observed = sorted(_date(str(row["opDate"])[:4]+"-"+str(row["opDate"])[4:6]+"-"+str(row["opDate"])[6:], field="official_calendar_date") for row in rows)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("official_business_dates_evidence_invalid") from None
        if (len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != source_hash
            or not isinstance(rows, list) or not rows or len(rows) > MAX_CALENDAR_DATES
            or not isinstance(page,dict) or page.get("pageCount") != 1 or page.get("total") != len(rows)
            or observed != dates or start > dates[0] or end < dates[-1] or start > end):
            raise ValueError("official_business_dates_evidence_invalid")
    return dates, source_hash


def validate_official_calendar(value: Any) -> tuple[list[str], str]:
    """Shared bounded calendar contract for planning and actual materialization."""
    return _calendar(value)


def _source_index(rows: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("source_evidence_invalid")
    if len(rows) > MAX_SOURCE_EVIDENCE:
        raise ValueError("source_evidence_limit_exceeded")
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("source_evidence_row_invalid")
        trade_date = _date(row.get("tradeDate"), field="source_trade_date")
        market = row.get("market")
        if market not in MARKETS:
            raise ValueError("source_evidence_market_invalid")
        key = (trade_date, market)
        if key in indexed:
            raise ValueError(f"source_evidence_duplicate:{trade_date}:{market}")
        raw = row.get("raw")
        normalized = row.get("normalized")
        if not isinstance(raw, dict) or not isinstance(normalized, dict):
            raise ValueError(f"source_evidence_stage_missing:{trade_date}:{market}")
        for stage, evidence in (("raw", raw), ("normalized", normalized)):
            if evidence.get("status") not in _STAGE_STATUS:
                raise ValueError(f"source_evidence_status_invalid:{stage}:{trade_date}:{market}")
        clean: dict[str, Any] = {"tradeDate": trade_date, "market": market}
        clean["raw"] = {"status": raw["status"]}
        clean["normalized"] = {"status": normalized["status"]}
        if raw["status"] == "AVAILABLE":
            clean["raw"].update(
                sha256=_sha256(raw.get("sha256"), field="raw_sha256"),
                bytes=_count(raw.get("bytes"), field="raw_bytes"),
            )
        if normalized["status"] == "AVAILABLE":
            clean["normalized"].update(
                sha256=_sha256(normalized.get("sha256"), field="normalized_sha256"),
                sourceSha256=_sha256(
                    normalized.get("sourceSha256"), field="normalized_source_sha256"
                ),
                bytes=_count(normalized.get("bytes"), field="normalized_bytes"),
                rowCount=_count(normalized.get("rowCount"), field="normalized_row_count"),
                shareRows=_count(normalized.get("shareRows"), field="normalized_share_rows"),
                navAlignedRows=_count(
                    normalized.get("navAlignedRows"), field="normalized_nav_aligned_rows"
                ),
            )
        if normalized["status"] == "AVAILABLE":
            facts = clean["normalized"]
            if facts["rowCount"] == 0 or facts["shareRows"] == 0:
                raise ValueError(f"normalized_etf_facts_empty:{trade_date}:{market}")
            if facts["shareRows"] > facts["rowCount"] or facts["navAlignedRows"] > facts["shareRows"]:
                raise ValueError(f"normalized_etf_counts_invalid:{trade_date}:{market}")
        indexed[key] = clean
    return indexed


def _model_index(rows: Any) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("existing_models_invalid")
    if len(rows) > MAX_EXISTING_MODELS:
        raise ValueError("existing_models_limit_exceeded")
    indexed: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("existing_model_row_invalid")
        kind = row.get("kind")
        if kind not in {"daily_change", "period"}:
            raise ValueError("existing_model_kind_invalid")
        as_of = _date(row.get("asOf"), field="existing_model_date")
        period = row.get("period")
        if kind == "period" and period not in MAX_PERIODS:
            raise ValueError("existing_model_period_invalid")
        if kind == "daily_change" and period is not None:
            raise ValueError("existing_daily_model_period_invalid")
        metric = row.get("metric")
        scope = row.get("sampleScope")
        if metric not in PERIOD_METRICS or scope not in SAMPLE_SCOPES:
            raise ValueError("existing_model_identity_invalid")
        key = (kind, as_of, str(period or ""), metric, scope)
        if key in indexed:
            raise ValueError(f"existing_model_duplicate:{':'.join(key)}")
        status = row.get("status")
        if status not in {"READY", "PARTIAL", "MISSING", "UNKNOWN", "BLOCKED_QUALITY"}:
            raise ValueError("existing_model_status_invalid")
        fingerprint = row.get("inputFingerprint")
        if fingerprint is not None:
            fingerprint = _sha256(fingerprint, field="existing_model_input_fingerprint")
        indexed[key] = {"status": status, "inputFingerprint": fingerprint}
    return indexed


def _window_for_target(
    calendar_dates: list[str],
    date_index: dict[str, int],
    target: str,
    period: str,
) -> dict[str, Any]:
    target_index = date_index[target]
    if period == "ytd":
        year_dates = [item for item in calendar_dates[: target_index + 1] if item[:4] == target[:4]]
        if not year_dates:
            return {"status": "BLOCKED_CALENDAR", "reasonCode": "ytd_calendar_window_missing"}
        first_index = date_index[year_dates[0]]
        if first_index == 0:
            return {
                "status": "BLOCKED_CALENDAR",
                "reasonCode": "ytd_official_baseline_missing",
                "requiredBaselineCount": 1,
            }
        return {
            "status": "READY",
            "baselineDate": calendar_dates[first_index - 1],
            "snapshotDates": calendar_dates[first_index - 1 : target_index + 1],
            "dailyChangeDates": calendar_dates[first_index : target_index + 1],
        }
    lookback = PERIOD_LOOKBACKS[period]
    if target_index < lookback:
        return {
            "status": "BLOCKED_CALENDAR",
            "reasonCode": "official_calendar_baseline_missing",
            "requiredSnapshotCount": lookback + 1,
            "availablePriorSnapshotCount": target_index + 1,
        }
    start_index = target_index - lookback
    return {
        "status": "READY",
        "baselineDate": calendar_dates[start_index],
        "snapshotDates": calendar_dates[start_index : target_index + 1],
        "dailyChangeDates": calendar_dates[start_index + 1 : target_index + 1],
    }


def _source_state(
    evidence: dict[tuple[str, str], dict[str, Any]],
    *,
    trade_date: str,
    market: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    item = evidence.get((trade_date, market))
    if item is None:
        return None, [
            {
                "tradeDate": trade_date,
                "market": market,
                "stage": "raw",
                "status": "MISSING",
                "reasonCode": "source_evidence_absent",
                "action": "VERIFY_ARCHIVED_SOURCE_OR_MARK_UNAVAILABLE",
            }
        ]
    raw = item["raw"]
    normalized = item["normalized"]
    issues: list[dict[str, Any]] = []
    if raw["status"] != "AVAILABLE":
        issues.append(
            {
                "tradeDate": trade_date,
                "market": market,
                "stage": "raw",
                "status": raw["status"],
                "reasonCode": "raw_source_not_available",
                "action": "VERIFY_ARCHIVED_SOURCE_OR_MARK_UNAVAILABLE",
            }
        )
    if normalized["status"] != "AVAILABLE":
        issues.append(
            {
                "tradeDate": trade_date,
                "market": market,
                "stage": "normalized",
                "status": normalized["status"],
                "reasonCode": "normalized_fact_not_available",
                "action": (
                    "NORMALIZE_FROM_ARCHIVED_RAW"
                    if raw["status"] == "AVAILABLE"
                    else "WAIT_FOR_RAW_SOURCE"
                ),
            }
        )
    if issues:
        return item, issues
    if normalized["sourceSha256"] != raw["sha256"]:
        return item, [
            {
                "tradeDate": trade_date,
                "market": market,
                "stage": "lineage",
                "status": "BLOCKED_QUALITY",
                "reasonCode": "normalized_source_hash_mismatch",
                "action": "REPAIR_SOURCE_LINEAGE_BEFORE_BUILD",
            }
        ]
    return item, []


def _source_dependencies(
    evidence: dict[tuple[str, str], dict[str, Any]],
    *,
    snapshot_dates: list[str],
    daily_change_dates: list[str],
    metric: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str | None, str]:
    descriptors: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    coverage_gaps: list[dict[str, Any]] = []
    partial = False
    for trade_date in snapshot_dates:
        for market in MARKETS:
            item, source_issues = _source_state(evidence, trade_date=trade_date, market=market)
            issues.extend(source_issues)
            if item is None or source_issues:
                continue
            normalized = item["normalized"]
            descriptor = {
                "tradeDate": trade_date,
                "market": market,
                "rawSha256": item["raw"]["sha256"],
                "normalizedSha256": normalized["sha256"],
                "shareRows": normalized["shareRows"],
            }
            if trade_date in daily_change_dates and metric == "estimatedAmount":
                nav_rows = normalized["navAlignedRows"]
                if nav_rows == 0:
                    issues.append(
                        {
                            "tradeDate": trade_date,
                            "market": market,
                            "stage": "normalized_nav",
                            "status": "MISSING",
                            "reasonCode": "same_date_nav_unavailable",
                            "action": "VERIFY_NAV_DATE_AND_NORMALIZED_FACTS",
                        }
                    )
                elif nav_rows < normalized["shareRows"]:
                    partial = True
                    coverage_gaps.append(
                        {
                            "tradeDate": trade_date,
                            "market": market,
                            "stage": "normalized_nav",
                            "status": "PARTIAL",
                            "reasonCode": "same_date_nav_coverage_incomplete",
                            "availableRows": nav_rows,
                            "expectedRows": normalized["shareRows"],
                            "action": "BUILD_PARTIAL_COVERAGE_ONLY",
                        }
                    )
                descriptor["navAlignedRows"] = nav_rows
            descriptors.append(descriptor)
    if issues:
        return descriptors, issues, coverage_gaps, None, "BLOCKED"
    source_fingerprint = _canonical_hash(descriptors)
    return (
        descriptors,
        issues,
        coverage_gaps,
        source_fingerprint,
        "PARTIAL" if partial else "READY",
    )


def _model_decision(
    *,
    kind: str,
    as_of: str,
    period: str | None,
    metric: str,
    scope: str,
    expected_status: str,
    input_fingerprint: str | None,
    models: dict[tuple[str, str, str, str, str], dict[str, Any]],
) -> tuple[str, str | None]:
    if expected_status == "BLOCKED":
        return "BLOCKED", "source_dependency_missing"
    key = (kind, as_of, str(period or ""), metric, scope)
    existing = models.get(key)
    if (
        existing is not None
        and existing["status"] == expected_status
        and existing["inputFingerprint"] == input_fingerprint
    ):
        return "REUSE", None
    if expected_status == "PARTIAL":
        return "BUILD_PARTIAL", "same_date_nav_coverage_incomplete"
    return "BUILD", "model_missing_or_stale"


def build_etf_period_rebuild_plan(
    *,
    calendar: dict[str, Any],
    target_dates: Iterable[str],
    source_evidence: list[dict[str, Any]],
    existing_models: list[dict[str, Any]],
    classification_version: str | None = None,
    formula_versions: dict[str, str] | None = None,
    periods: Iterable[str] = MAX_PERIODS,
) -> dict[str, Any]:
    """Return a deterministic, bounded source-to-period repair plan.

    A target period of N official sessions includes exactly N adjacent daily
    changes and therefore N+1 official snapshot dates (one baseline). YTD
    includes the preceding official session before the first session in the
    target year. Calendar gaps, source gaps, stale formulas and classification
    changes remain explicit; no dates or values are synthesized.
    """

    calendar_dates, calendar_hash = _calendar(calendar)
    raw_targets = _bounded_values(
        target_dates, limit=MAX_TARGET_DATES, field="target_dates"
    )
    targets = [_date(value, field="target_date") for value in raw_targets]
    if not targets:
        raise ValueError("target_dates_missing")
    if len(targets) > MAX_TARGET_DATES:
        raise ValueError("target_dates_limit_exceeded")
    if len(set(targets)) != len(targets):
        raise ValueError("target_dates_duplicate")
    targets = sorted(targets)
    if any(value not in set(calendar_dates) for value in targets):
        raise ValueError("target_date_not_in_official_calendar")
    selected_periods = tuple(
        _bounded_values(periods, limit=len(MAX_PERIODS), field="period_selection")
    )
    if (
        not selected_periods
        or len(set(selected_periods)) != len(selected_periods)
        or any(value not in MAX_PERIODS for value in selected_periods)
    ):
        raise ValueError("period_selection_invalid")
    selected_periods = tuple(value for value in MAX_PERIODS if value in selected_periods)
    if classification_version is None:
        classification_version = CLASSIFICATION_VERSION
    if not isinstance(classification_version, str) or not classification_version.strip():
        raise ValueError("classification_version_missing")
    formulas = dict(FORMULA_VERSIONS if formula_versions is None else formula_versions)
    if set(formulas) != set(PERIOD_METRICS) or any(
        not isinstance(value, str) or not value.strip() for value in formulas.values()
    ):
        raise ValueError("formula_versions_invalid")

    evidence = _source_index(source_evidence)
    models = _model_index(existing_models)
    date_index = {value: index for index, value in enumerate(calendar_dates)}
    windows: list[dict[str, Any]] = []
    daily_requirements: dict[tuple[str, str], dict[str, Any]] = {}
    blocked_calendar_nodes = 0
    for target in targets:
        for period in selected_periods:
            window = _window_for_target(calendar_dates, date_index, target, period)
            windows.append(
                {
                    "asOf": target,
                    "period": period,
                    **window,
                }
            )
            if window["status"] != "READY":
                blocked_calendar_nodes += len(PERIOD_METRICS) * len(SAMPLE_SCOPES)
                continue
            for current in window["dailyChangeDates"]:
                previous = calendar_dates[date_index[current] - 1]
                daily_requirements[(current, previous)] = {
                    "asOf": current,
                    "previousDate": previous,
                }

    daily_nodes: list[dict[str, Any]] = []
    daily_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing_sources: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for (current, previous), _ in sorted(daily_requirements.items()):
        for metric in PERIOD_METRICS:
            snapshot_dates = [previous, current]
            descriptors, issues, coverage_gaps, source_fp, dependency_status = _source_dependencies(
                evidence,
                snapshot_dates=snapshot_dates,
                daily_change_dates=[current],
                metric=metric,
            )
            for issue in issues:
                missing_sources[(issue["tradeDate"], issue["market"], issue["stage"], issue["reasonCode"])] = issue
            for issue in coverage_gaps:
                missing_sources[(issue["tradeDate"], issue["market"], issue["stage"], issue["reasonCode"])] = issue
            for scope in SAMPLE_SCOPES:
                input_fp = None
                if source_fp is not None:
                    input_fp = _canonical_hash(
                        {
                            "contractVersion": PLAN_CONTRACT_VERSION,
                            "kind": "daily_change",
                            "asOf": current,
                            "previousDate": previous,
                            "metric": metric,
                            "sampleScope": scope,
                            "calendarWindow": {
                                "sourceId": OFFICIAL_CALENDAR_SOURCE,
                                "sessionDates": [previous, current],
                            },
                            "classificationVersion": classification_version,
                            "formulaVersion": formulas[metric],
                            "sourceFingerprint": source_fp,
                        }
                    )
                expected_status = "BLOCKED" if dependency_status == "BLOCKED" else dependency_status
                decision, reason = _model_decision(
                    kind="daily_change",
                    as_of=current,
                    period=None,
                    metric=metric,
                    scope=scope,
                    expected_status=expected_status,
                    input_fingerprint=input_fp,
                    models=models,
                )
                node = {
                    "kind": "daily_change",
                    "asOf": current,
                    "previousDate": previous,
                    "metric": metric,
                    "sampleScope": scope,
                    "dependencyStatus": expected_status,
                    "decision": decision,
                    "reasonCode": reason,
                    "classificationVersion": classification_version,
                    "formulaVersion": formulas[metric],
                    "inputFingerprint": input_fp,
                    "sourceFingerprint": source_fp,
                    "sourceDependencies": descriptors,
                }
                daily_nodes.append(node)
                daily_by_identity[(current, metric, scope)] = node

    period_nodes: list[dict[str, Any]] = []
    for window in windows:
        if window["status"] != "READY":
            for metric in PERIOD_METRICS:
                for scope in SAMPLE_SCOPES:
                    period_nodes.append(
                        {
                            "kind": "period",
                            "asOf": window["asOf"],
                            "period": window["period"],
                            "metric": metric,
                            "sampleScope": scope,
                            "dependencyStatus": "BLOCKED_CALENDAR",
                            "decision": "BLOCKED",
                            "reasonCode": window["reasonCode"],
                            "inputFingerprint": None,
                            "windowRef": {
                                "asOf": window["asOf"],
                                "period": window["period"],
                            },
                        }
                    )
            continue
        for metric in PERIOD_METRICS:
            for scope in SAMPLE_SCOPES:
                dependencies = [
                    daily_by_identity[(trade_date, metric, scope)]
                    for trade_date in window["dailyChangeDates"]
                ]
                dependency_statuses = {item["dependencyStatus"] for item in dependencies}
                if "BLOCKED" in dependency_statuses:
                    expected_status = "BLOCKED"
                elif "PARTIAL" in dependency_statuses:
                    expected_status = "PARTIAL"
                else:
                    expected_status = "READY"
                input_fp = None
                if expected_status != "BLOCKED":
                    input_fp = _canonical_hash(
                        {
                            "contractVersion": PLAN_CONTRACT_VERSION,
                            "kind": "period",
                            "asOf": window["asOf"],
                            "period": window["period"],
                            "metric": metric,
                            "sampleScope": scope,
                            "calendarWindow": {
                                "sourceId": OFFICIAL_CALENDAR_SOURCE,
                                "sessionDates": window["snapshotDates"],
                            },
                            "classificationVersion": classification_version,
                            "formulaVersion": formulas[metric],
                            "dailyChanges": [
                                {"asOf": item["asOf"], "inputFingerprint": item["inputFingerprint"]}
                                for item in dependencies
                            ],
                        }
                    )
                decision, reason = _model_decision(
                    kind="period",
                    as_of=window["asOf"],
                    period=window["period"],
                    metric=metric,
                    scope=scope,
                    expected_status=expected_status,
                    input_fingerprint=input_fp,
                    models=models,
                )
                period_nodes.append(
                    {
                        "kind": "period",
                        "asOf": window["asOf"],
                        "period": window["period"],
                        "metric": metric,
                        "sampleScope": scope,
                        "dependencyStatus": expected_status,
                        "decision": decision,
                        "reasonCode": reason,
                        "classificationVersion": classification_version,
                        "formulaVersion": formulas[metric],
                        "inputFingerprint": input_fp,
                        "windowRef": {
                            "asOf": window["asOf"],
                            "period": window["period"],
                        },
                        "dailyChangeDependencyCount": len(dependencies),
                    }
                )

    daily_nodes.sort(key=lambda item: (item["asOf"], item["metric"], item["sampleScope"]))
    period_nodes.sort(
        key=lambda item: (
            item["asOf"],
            MAX_PERIODS.index(item["period"]),
            item["metric"],
            item["sampleScope"],
        )
    )
    source_gaps = sorted(
        missing_sources.values(),
        key=lambda item: (item["tradeDate"], item["market"], item["stage"], item["reasonCode"]),
    )
    build_nodes = [
        item
        for item in daily_nodes + period_nodes
        if item["decision"] in {"BUILD", "BUILD_PARTIAL"}
    ]
    blocked_nodes = sum(
        item["decision"] in {"BLOCKED", "BLOCKED_CALENDAR"}
        for item in daily_nodes + period_nodes
    )
    plan_status = "BLOCKED" if blocked_nodes else ("PLAN_READY" if build_nodes else "UP_TO_DATE")
    operation_counts = {
        decision: sum(
            item["decision"] in (
                {"BLOCKED", "BLOCKED_CALENDAR"}
                if decision == "BLOCKED"
                else {decision}
            )
            for item in daily_nodes + period_nodes
        )
        for decision in ("BUILD", "BUILD_PARTIAL", "REUSE", "BLOCKED")
    }
    plan: dict[str, Any] = {
        "schemaVersion": 1,
        "contractVersion": PLAN_CONTRACT_VERSION,
        "executionStatus": "NOT_RUN",
        "planStatus": plan_status,
        "scope": "candidate_only_offline_plan",
        "targetDates": targets,
        "periods": list(selected_periods),
        "calendar": {
            "sourceId": OFFICIAL_CALENDAR_SOURCE,
            "sha256": calendar_hash,
            "from": calendar_dates[0],
            "to": calendar_dates[-1],
            "dateCount": len(calendar_dates),
        },
        "versions": {
            "classification": classification_version,
            "formulas": {metric: formulas[metric] for metric in PERIOD_METRICS},
        },
        "bounds": {
            "maxTargetDates": MAX_TARGET_DATES,
            "maxCalendarDates": MAX_CALENDAR_DATES,
            "maxSourceEvidenceRows": MAX_SOURCE_EVIDENCE,
            "maxExistingModelRows": MAX_EXISTING_MODELS,
        },
        "windows": windows,
        "sourceGaps": source_gaps,
        "dailyChangeNodes": daily_nodes,
        "periodNodes": period_nodes,
        "summary": {
            "targetDateCount": len(targets),
            "periodWindowCount": len(windows),
            "dailyChangeNodeCount": len(daily_nodes),
            "periodNodeCount": len(period_nodes),
            "calendarBlockedNodeCount": blocked_calendar_nodes,
            "sourceGapCount": len(source_gaps),
            "blockedNodeCount": blocked_nodes,
            "buildNodeCount": operation_counts["BUILD"],
            "partialBuildNodeCount": operation_counts["BUILD_PARTIAL"],
            "reusedNodeCount": operation_counts["REUSE"],
        },
        "resourceEstimate": {
            "plannerRemoteReads": 0,
            "plannerRemoteWrites": 0,
            "plannerD1RowsRead": 0,
            "plannerD1RowsWritten": 0,
            "plannerR2Gets": 0,
            "plannerR2Puts": 0,
            "futureBuildCostStatus": "UNKNOWN",
            "futureBuildCostReasonCode": "execution_telemetry_not_supplied",
        },
    }
    plan["planHash"] = _canonical_hash(plan)
    return plan
