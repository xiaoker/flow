"""Versioned, evidence-backed capability states for candidate read models.

The v0.7 contract describes what one immutable release slot can prove. A
dashboard manifest never implies that the independently published ETF
penetration slot is ready; that slot remains UNKNOWN until a later consumer
binds its exact release. These helpers are offline and do not access storage.
"""

from __future__ import annotations

import math
import re
from typing import Any


CAPABILITY_CONTRACT_VERSION = "flow-capability-v0.7.0"
CAPABILITY_MANIFEST_SCHEMA_VERSION = 4
CAPABILITY_IDS = (
    "etf_daily_core",
    "etf_radar",
    "rotation",
    "national_team",
    "connect",
    "margin",
    "etf_periods",
    "etf_penetration",
    "stock_search",
    "stock_detail",
)
ETF_PERIOD_IDS = ("1d", "3d", "1w", "2w", "1m", "3m", "6m", "ytd", "12m")
ETF_PERIOD_METRICS = ("shares", "estimatedAmount")
PERIOD_DAY_WINDOWS = {
    "1d": 1,
    "3d": 3,
    "1w": 5,
    "2w": 10,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "12m": 252,
}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CAPABILITY_SLOT: dict[str, str] = {
    "etf_daily_core": "dashboard",
    "etf_radar": "dashboard",
    "rotation": "dashboard",
    "national_team": "dashboard",
    "connect": "dashboard",
    "margin": "dashboard",
    "etf_periods": "dashboard",
    "etf_penetration": "etf_penetration",
    "stock_search": "etf_penetration",
    "stock_detail": "etf_penetration",
}

CAPABILITY_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "etf_daily_core": ("bootstrap", "radar", "rotation"),
    "etf_radar": ("radar",),
    "rotation": ("rotation",),
    "national_team": ("national",),
    "connect": ("connect",),
    "margin": ("margin",),
    "etf_periods": ("radar", "rotation"),
    "etf_penetration": ("radar_flow", "pressure"),
    "stock_search": ("stock_search",),
    "stock_detail": tuple(f"stock_{index}" for index in range(8)),
}


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        from datetime import date

        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _expected_period_days(period: str, terminal: dict[str, Any], as_of: str) -> int | None:
    evidence = terminal.get("windowEvidence")
    if isinstance(evidence, dict):
        window = evidence.get("periods", {}).get(period, {})
        count = window.get("expectedChanges")
        return count if isinstance(count,int) and not isinstance(count,bool) and count>0 else None
    fixed = PERIOD_DAY_WINDOWS.get(period)
    if fixed is not None:
        return fixed
    if period != "ytd":
        return None
    daily = terminal.get("dailySeries")
    if not isinstance(daily, list):
        return None
    month_days = [
        row.get("date")
        for row in daily
        if isinstance(row, dict) and isinstance(row.get("date"), str) and re.fullmatch(r"\d{2}-\d{2}", row["date"])
    ]
    wraps = [index for index in range(1, len(month_days)) if month_days[index] < month_days[index - 1]]
    if not wraps:
        return None
    return len(month_days) - wraps[-1]


def _source_status(snapshot: dict[str, Any], source_name: str) -> str | None:
    for item in snapshot.get("dataStatus") or []:
        if isinstance(item, dict) and str(item.get("name", "")).strip() == source_name:
            value = item.get("status")
            return value.strip().lower() if isinstance(value, str) else None
    return None


def _source_dates_for(domain: str, source_dates: dict[str, Any]) -> dict[str, str | None]:
    fields = {
        "core": ("snapshot", "sseEtfAsOf", "szseEtfAsOf"),
        "connect": ("connectAsOf",),
        "margin": ("marginAsOf",),
        "penetration": ("pcfEffectiveAsOf", "pcfAsOf"),
        "etf": ("sseEtfAsOf", "szseEtfAsOf"),
    }.get(domain, ())
    return {field: source_dates.get(field) for field in fields}


def _entry(
    *,
    status: str,
    as_of: str | None,
    source_dates: dict[str, str | None],
    artifact_names: tuple[str, ...],
    reason_code: str | None,
    coverage: dict[str, int] | None = None,
    periods: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "asOf": as_of,
        "sourceDates": source_dates,
        "artifactNames": list(artifact_names),
        "reasonCode": reason_code,
        "coverage": coverage or {},
    }
    if periods is not None:
        result["periods"] = periods
    return result


def _unknown_unbound() -> dict[str, Any]:
    return _entry(
        status="UNKNOWN",
        as_of=None,
        source_dates={},
        artifact_names=(),
        reason_code="separate_slot_not_bound",
    )


def _date_gate(
    *,
    publication_confirmed: bool,
    source_date: str | None,
    as_of: str,
    source_status: str | None,
) -> tuple[str, str | None]:
    if not publication_confirmed:
        return "WAITING_SOURCES", "publication_not_confirmed"
    if source_date is None:
        return "SOURCE_UNAVAILABLE", "source_date_unproven"
    if source_date != as_of:
        return "SOURCE_UNAVAILABLE", "source_date_mismatch"
    if source_status != "ready":
        return "SOURCE_UNAVAILABLE", "source_status_not_ready"
    return "READY", None


def _period_states(terminal: dict[str, Any], as_of: str) -> tuple[str, dict[str, Any], dict[str, int]]:
    items = terminal.get("instruments")
    items = items if isinstance(items, list) else []
    available_periods = terminal.get("availablePeriods")
    available_periods = {value for value in available_periods if isinstance(value, str)} if isinstance(available_periods, list) else set()
    expected_samples = len(items)
    windows: dict[str, Any] = {}
    ready_count = 0
    present_count = 0
    unknown_count = 0

    for period in ETF_PERIOD_IDS:
        metric_states: dict[str, Any] = {}
        for metric, value_field, coverage_field in (
            ("shares", "periods", "periodCoverage"),
            ("estimatedAmount", "estimatedCashPeriods", "cashPeriodCoverage"),
        ):
            available_samples = 0
            evidence_invalid = False
            if period in available_periods:
                expected_days = _expected_period_days(period, terminal, as_of)
                for item in items:
                    if not isinstance(item, dict):
                        evidence_invalid = True
                        continue
                    values = item.get(value_field)
                    coverage = item.get(coverage_field)
                    if not isinstance(values, dict) or not isinstance(coverage, dict):
                        evidence_invalid = True
                        continue
                    count = coverage.get(period)
                    value = values.get(period)
                    if (
                        not isinstance(count, int)
                        or isinstance(count, bool)
                        or count < 0
                        or (expected_days is not None and count > expected_days)
                        or (expected_days is None and (period == "ytd" or isinstance(terminal.get("windowEvidence"),dict)))
                        or (_is_finite_number(value) and (count == 0 or (expected_days is not None and count != expected_days)))
                        or (value is None and expected_days is not None and count == expected_days)
                        or (value is not None and not _is_finite_number(value))
                    ):
                        evidence_invalid = True
                    if _is_finite_number(value):
                        available_samples += 1
            else:
                for item in items:
                    if not isinstance(item, dict):
                        evidence_invalid = True
                        continue
                    values = item.get(value_field)
                    coverage = item.get(coverage_field)
                    if (
                        isinstance(values, dict) and period in values
                    ) or (
                        isinstance(coverage, dict) and period in coverage
                    ):
                        evidence_invalid = True
            missing_samples = max(0, expected_samples - available_samples)
            if evidence_invalid:
                status, reason_code = "UNKNOWN", "period_coverage_evidence_invalid"
            elif expected_samples == 0:
                status, reason_code = "UNKNOWN", "sample_universe_unavailable"
            elif available_samples == 0:
                status, reason_code = "MISSING", "period_has_no_comparable_samples"
            elif available_samples < expected_samples:
                status, reason_code = "PARTIAL", "period_sample_coverage_incomplete"
            else:
                status, reason_code = "READY", None
            # Read the materialized per-item count only as a consistency check;
            # the capability count above is recomputed from the actual values.
            declared_counts = [
                item.get(coverage_field, {}).get(period)
                for item in items
                if isinstance(item, dict) and isinstance(item.get(coverage_field), dict)
            ]
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in declared_counts
            ):
                status, reason_code = "UNKNOWN", "period_coverage_evidence_invalid"
            metric_states[metric] = {
                "status": status,
                "sampleCount": expected_samples,
                "availableSampleCount": available_samples,
                "missingSampleCount": missing_samples,
                "reasonCode": reason_code,
            }
            if status == "READY":
                ready_count += 1
            if status in {"READY", "PARTIAL"}:
                present_count += 1
            if status == "UNKNOWN":
                unknown_count += 1
        windows[period] = metric_states

    expected_metric_count = len(ETF_PERIOD_IDS) * len(ETF_PERIOD_METRICS)
    if expected_samples == 0:
        overall = "UNKNOWN"
        reason = "sample_universe_unavailable"
    elif unknown_count:
        overall = "UNKNOWN"
        reason = "period_coverage_evidence_invalid"
    elif ready_count == expected_metric_count:
        overall = "READY"
        reason = None
    elif present_count:
        overall = "PARTIAL"
        reason = "one_or_more_period_metric_windows_incomplete"
    else:
        overall = "MISSING"
        reason = "no_period_metric_window_available"
    return overall, windows, {
        "periodCount": len(ETF_PERIOD_IDS),
        "readyPeriodMetricCount": ready_count,
        "expectedPeriodMetricCount": expected_metric_count,
    }


def _is_nonnegative_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _period_contract_is_consistent(state: dict[str, Any]) -> bool:
    """Mirror the Worker period matrix checks before a full contract is split."""

    periods = state.get("periods")
    coverage = state.get("coverage")
    if (
        not isinstance(periods, dict)
        or set(periods) != set(ETF_PERIOD_IDS)
        or not isinstance(coverage, dict)
        or coverage.get("periodCount") != len(ETF_PERIOD_IDS)
        or coverage.get("expectedPeriodMetricCount") != len(ETF_PERIOD_IDS) * len(ETF_PERIOD_METRICS)
    ):
        return False

    ready_count = 0
    present_count = 0
    unknown_count = 0
    for period in ETF_PERIOD_IDS:
        metrics = periods.get(period)
        if not isinstance(metrics, dict) or set(metrics) != set(ETF_PERIOD_METRICS):
            return False
        for metric in ETF_PERIOD_METRICS:
            value = metrics.get(metric)
            if not isinstance(value, dict):
                return False
            status = value.get("status")
            sample_count = value.get("sampleCount")
            available = value.get("availableSampleCount")
            missing = value.get("missingSampleCount")
            reason = value.get("reasonCode")
            if (
                status not in {"READY", "PARTIAL", "MISSING", "UNKNOWN"}
                or not all(_is_nonnegative_count(count) for count in (sample_count, available, missing))
                or available + missing != sample_count
                or (reason is not None and (not isinstance(reason, str) or not reason))
            ):
                return False
            if status == "READY":
                if sample_count == 0 or available != sample_count or missing != 0 or reason is not None:
                    return False
                ready_count += 1
                present_count += 1
            elif status == "PARTIAL":
                if sample_count == 0 or available == 0 or available >= sample_count or reason is None:
                    return False
                present_count += 1
            elif status == "MISSING":
                if sample_count == 0 or available != 0 or reason is None:
                    return False
            else:
                if reason is None:
                    return False
                unknown_count += 1

    if (
        not _is_nonnegative_count(coverage.get("readyPeriodMetricCount"))
        or coverage["readyPeriodMetricCount"] != ready_count
    ):
        return False
    expected = len(ETF_PERIOD_IDS) * len(ETF_PERIOD_METRICS)
    status = state.get("status")
    if status == "READY":
        return ready_count == expected and state.get("reasonCode") is None
    if status == "PARTIAL":
        return present_count > 0 and ready_count < expected and unknown_count == 0 and isinstance(state.get("reasonCode"), str)
    if status == "MISSING":
        return present_count == 0 and unknown_count == 0 and isinstance(state.get("reasonCode"), str)
    if status == "UNKNOWN":
        return unknown_count > 0 and isinstance(state.get("reasonCode"), str)
    return False


def _period_reason(status: str, periods: dict[str, Any]) -> str:
    if status != "UNKNOWN":
        return "period_windows_incomplete"
    reasons = {
        metric_state.get("reasonCode")
        for window in periods.values()
        if isinstance(window, dict)
        for metric_state in window.values()
        if isinstance(metric_state, dict)
        and metric_state.get("status") == "UNKNOWN"
        and isinstance(metric_state.get("reasonCode"), str)
    }
    if len(reasons) == 1:
        return next(iter(reasons))
    if reasons:
        return "multiple_period_evidence_issues"
    return "period_evidence_unknown"


def build_capability_states(
    *,
    scope: str,
    as_of: str,
    source_dates: dict[str, Any],
    snapshot: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
    stock_catalog: dict[str, Any] | None,
    artifact_names: set[str],
) -> dict[str, Any]:
    """Build deterministic capability states before release IDs are assigned."""

    if scope not in {"dashboard", "etf_penetration", "full"}:
        raise ValueError("capability_scope_invalid")
    local_ids = {key for key, owner in CAPABILITY_SLOT.items() if scope == "full" or owner == scope}
    states: dict[str, Any] = {key: _unknown_unbound() for key in CAPABILITY_IDS}
    publication = snapshot.get("publication")
    publication_confirmed = isinstance(publication, dict) and publication.get("status") == "confirmed"
    terminal = snapshot.get("etfTerminal") if isinstance(snapshot.get("etfTerminal"), dict) else {}
    instruments = terminal.get("instruments") if isinstance(terminal.get("instruments"), list) else []
    history = terminal.get("historyCoverage") if isinstance(terminal.get("historyCoverage"), dict) else {}
    core_source_dates = _source_dates_for("core", source_dates)
    core_ready = (
        publication_confirmed
        and _source_status(snapshot, "上交所 ETF") == "ready"
        and _source_status(snapshot, "深交所 ETF") == "ready"
        and core_source_dates.get("sseEtfAsOf") == as_of
        and core_source_dates.get("szseEtfAsOf") == as_of
        and history.get("to") == as_of
        and isinstance(history.get("tradingDays"), int)
        and not isinstance(history.get("tradingDays"), bool)
        and history.get("tradingDays", 0) >= 2
        and bool(instruments)
    )
    core_status = "READY" if core_ready else (
        "WAITING_SOURCES" if not publication_confirmed else "SOURCE_UNAVAILABLE"
    )
    core_reason = None if core_ready else (
        "publication_not_confirmed" if not publication_confirmed else "core_source_or_history_proof_incomplete"
    )

    if "etf_daily_core" in local_ids:
        states["etf_daily_core"] = _entry(
            status=core_status,
            as_of=as_of if core_ready else None,
            source_dates=core_source_dates,
            artifact_names=CAPABILITY_ARTIFACTS["etf_daily_core"],
            reason_code=core_reason,
            coverage={
                "instrumentCount": len(instruments),
                "tradingDays": history.get("tradingDays", 0) if isinstance(history.get("tradingDays"), int) else 0,
            },
        )

    for capability_id, artifact_name in (
        ("etf_radar", "radar"),
        ("rotation", "rotation"),
        ("national_team", "national"),
    ):
        if capability_id not in local_ids:
            continue
        payload = payloads.get(artifact_name, {})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not core_ready:
            status, reason = "WAITING_SOURCES", "etf_daily_core_not_ready"
        elif capability_id == "etf_radar" and not instruments:
            status, reason = "MISSING", "etf_universe_empty"
        elif capability_id == "rotation" and not isinstance(data.get("groupPeriods"), list):
            status, reason = "MISSING", "rotation_payload_missing"
        elif capability_id == "national_team" and not isinstance(data.get("etfTerminal"), dict):
            status, reason = "MISSING", "national_team_payload_missing"
        else:
            status, reason = "READY", None
        count = len(data.get("etfTerminal", {}).get("instruments", [])) if capability_id == "national_team" and isinstance(data.get("etfTerminal"), dict) else (
            len(data.get("groupPeriods", [])) if capability_id == "rotation" and isinstance(data.get("groupPeriods"), list) else len(instruments)
        )
        if status == "READY" and count == 0:
            status = "MISSING"
            reason = f"{capability_id}_sample_universe_empty"
        states[capability_id] = _entry(
            status=status,
            as_of=as_of if status == "READY" else None,
            source_dates=_source_dates_for("etf", source_dates),
            artifact_names=CAPABILITY_ARTIFACTS[capability_id],
            reason_code=reason,
            coverage={"sampleCount": count},
        )

    for capability_id, source_name, date_key, artifact_name in (
        ("connect", "互联互通", "connectAsOf", "connect"),
        ("margin", "融资融券", "marginAsOf", "margin"),
    ):
        if capability_id not in local_ids:
            continue
        status, reason = _date_gate(
            publication_confirmed=publication_confirmed,
            source_date=source_dates.get(date_key),
            as_of=as_of,
            source_status=_source_status(snapshot, source_name),
        )
        value = payloads.get(artifact_name, {})
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        records = data.get("connect", []) if capability_id == "connect" else data.get("marginSeries", [])
        record_count = len(records) if isinstance(records, list) else 0
        if status == "READY" and record_count == 0:
            status, reason = "SOURCE_UNAVAILABLE", "source_payload_empty"
        states[capability_id] = _entry(
            status=status,
            as_of=as_of if status == "READY" else None,
            source_dates={date_key: source_dates.get(date_key)},
            artifact_names=CAPABILITY_ARTIFACTS[capability_id],
            reason_code=reason,
            coverage={"recordCount": record_count},
        )

    if "etf_periods" in local_ids:
        period_status, periods, coverage = _period_states(terminal, as_of) if core_ready else (
            "UNKNOWN",
            {
                period: {
                    metric: {
                        "status": "UNKNOWN",
                        "sampleCount": 0,
                        "availableSampleCount": 0,
                        "missingSampleCount": 0,
                        "reasonCode": "etf_daily_core_not_ready",
                    }
                    for metric in ETF_PERIOD_METRICS
                }
                for period in ETF_PERIOD_IDS
            },
            {"periodCount": len(ETF_PERIOD_IDS), "readyPeriodMetricCount": 0, "expectedPeriodMetricCount": len(ETF_PERIOD_IDS) * len(ETF_PERIOD_METRICS)},
        )
        states["etf_periods"] = _entry(
            status=period_status,
            as_of=as_of if period_status in {"READY", "PARTIAL"} else None,
            source_dates=_source_dates_for("etf", source_dates),
            artifact_names=CAPABILITY_ARTIFACTS["etf_periods"],
            reason_code=None if period_status == "READY" else _period_reason(period_status, periods),
            coverage=coverage,
            periods=periods,
        )

    if "etf_penetration" in local_ids:
        stock_flow = snapshot.get("etfStockFlow") if isinstance(snapshot.get("etfStockFlow"), dict) else {}
        basis = stock_flow.get("basis") if isinstance(stock_flow.get("basis"), dict) else {}
        catalog = stock_catalog if isinstance(stock_catalog, dict) else {}
        items = catalog.get("items") if isinstance(catalog.get("items"), list) else []
        coverage = catalog.get("coverage") if isinstance(catalog.get("coverage"), dict) else {}
        matched_etfs = coverage.get("matchedEtfCount", 0)
        pcf_dates = {
            "pcfEffectiveAsOf": basis.get("shareDate") if _is_date(basis.get("shareDate")) else None,
            "pcfAsOf": basis.get("pcfDate") if _is_date(basis.get("pcfDate")) else None,
        }
        catalog_basis = catalog.get("basis") if isinstance(catalog.get("basis"), dict) else {}
        search_payload = payloads.get("stock_search", {})
        search_items = search_payload.get("items") if isinstance(search_payload.get("items"), list) else []
        shard_items = sum(
            len(value.get("items", []))
            for name, value in payloads.items()
            if name.startswith("stock_") and name != "stock_search" and isinstance(value, dict) and isinstance(value.get("items"), list)
        )
        ready = (
            publication_confirmed
            and stock_flow.get("available") is True
            and basis.get("shareDate") == as_of
            and _is_date(basis.get("pcfDate"))
            and basis.get("pcfDate") == source_dates.get("pcfAsOf")
            and pcf_dates["pcfEffectiveAsOf"] == as_of
            and catalog.get("asOf") == as_of
            and catalog_basis.get("shareDate") == as_of
            and catalog_basis.get("pcfDate") == basis.get("pcfDate")
            and bool(items)
            and len(search_items) == len(items)
            and shard_items == len(items)
            and isinstance(matched_etfs, int)
            and not isinstance(matched_etfs, bool)
            and matched_etfs > 0
        )
        penetration_status = "READY" if ready else (
            "WAITING_SOURCES" if not publication_confirmed else (
                "SOURCE_UNAVAILABLE" if stock_flow.get("available") is not True else "MISSING"
            )
        )
        states["etf_penetration"] = _entry(
            status=penetration_status,
            as_of=as_of if ready else None,
            source_dates=pcf_dates,
            artifact_names=CAPABILITY_ARTIFACTS["etf_penetration"],
            reason_code=None if ready else (
                "publication_not_confirmed" if not publication_confirmed else (
                    "penetration_source_unavailable" if stock_flow.get("available") is not True else "penetration_model_incomplete"
                )
            ),
            coverage={"stockCount": len(items), "matchedEtfCount": matched_etfs if isinstance(matched_etfs, int) and matched_etfs >= 0 else 0},
        )
        for capability_id, names in (("stock_search", ("stock_search",)), ("stock_detail", CAPABILITY_ARTIFACTS["stock_detail"])):
            items_count = len(items)
            capable = ready and items_count > 0
            states[capability_id] = _entry(
                status="READY" if capable else (
                    "WAITING_SOURCES" if not publication_confirmed else (
                        "SOURCE_UNAVAILABLE" if not ready else "MISSING"
                    )
                ),
                as_of=as_of if capable else None,
                source_dates=pcf_dates,
                artifact_names=names,
                reason_code=None if capable else (
                    "publication_not_confirmed" if not publication_confirmed else (
                        "penetration_source_unavailable" if not ready else "stock_catalog_empty"
                    )
                ),
                coverage={"stockCount": items_count},
            )

    # The scope is deliberately explicit. Other-slot capabilities remain
    # UNKNOWN until a later candidate binds the exact peer release.
    for capability_id in CAPABILITY_IDS:
        if capability_id not in local_ids:
            states[capability_id] = _unknown_unbound()
        expected = CAPABILITY_ARTIFACTS[capability_id]
        if capability_id in local_ids and tuple(states[capability_id]["artifactNames"]) != expected:
            raise ValueError(f"capability_artifact_contract_invalid:{capability_id}")
        if capability_id in local_ids and not set(expected).issubset(artifact_names):
            raise ValueError(f"capability_artifact_missing:{capability_id}")
    return states


def capability_contract_identity(states: dict[str, Any], scope: str) -> dict[str, Any]:
    """Return release-ID input without cyclic object keys."""

    return {
        "version": CAPABILITY_CONTRACT_VERSION,
        "scope": scope,
        "capabilities": {
            capability_id: {
                **{key: value for key, value in states[capability_id].items() if key != "artifactNames"},
                "artifactNames": list(states[capability_id].get("artifactNames", [])),
            }
            for capability_id in CAPABILITY_IDS
        },
    }


def bind_capability_artifact_refs(
    states: dict[str, Any],
    scope: str,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Bind capability states to exact immutable descriptors in one manifest."""

    capabilities: dict[str, Any] = {}
    for capability_id in CAPABILITY_IDS:
        state = states[capability_id]
        names = state.get("artifactNames", [])
        refs = []
        for name in names:
            descriptor = artifacts.get(name)
            if not isinstance(descriptor, dict):
                raise ValueError(f"capability_artifact_descriptor_missing:{capability_id}:{name}")
            refs.append(
                {
                    "name": name,
                    "key": descriptor["key"],
                    "sha256": descriptor["sha256"],
                    "bytes": descriptor["bytes"],
                }
            )
        capabilities[capability_id] = {
            **{key: value for key, value in state.items() if key != "artifactNames"},
            "artifactRefs": refs,
        }
    return {"version": CAPABILITY_CONTRACT_VERSION, "scope": scope, "capabilities": capabilities}


def states_from_capability_contract(contract: dict[str, Any], scope: str) -> dict[str, Any]:
    """Project a full candidate contract to a slot without carrying stale refs."""

    if contract.get("version") != CAPABILITY_CONTRACT_VERSION:
        raise ValueError("capability_contract_version_unsupported")
    source = contract.get("capabilities")
    if not isinstance(source, dict):
        raise ValueError("capability_contract_missing_states")
    local_ids = {key for key, owner in CAPABILITY_SLOT.items() if scope == "full" or owner == scope}
    states: dict[str, Any] = {}
    for capability_id in CAPABILITY_IDS:
        if capability_id not in local_ids:
            states[capability_id] = _unknown_unbound()
            continue
        existing = source.get(capability_id)
        if not isinstance(existing, dict):
            raise ValueError(f"capability_state_missing:{capability_id}")
        states[capability_id] = {
            **{key: value for key, value in existing.items() if key != "artifactRefs"},
            "artifactNames": list(CAPABILITY_ARTIFACTS[capability_id]),
        }
    return states


def validate_capability_contract_for_split(contract: Any, manifest: dict[str, Any]) -> None:
    """Fail closed before a full v4 manifest is re-emitted into a slot."""

    if (
        not isinstance(contract, dict)
        or contract.get("version") != CAPABILITY_CONTRACT_VERSION
        or contract.get("scope") != "full"
        or not isinstance(contract.get("capabilities"), dict)
        or set(contract["capabilities"]) != set(CAPABILITY_IDS)
    ):
        raise ValueError("source manifest capability contract is invalid")

    capabilities = contract["capabilities"]
    as_of = manifest.get("asOf")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("source manifest artifacts are invalid")
    for capability_id in CAPABILITY_IDS:
        state = capabilities.get(capability_id)
        if not isinstance(state, dict):
            raise ValueError(f"source capability state is invalid: {capability_id}")
        status = state.get("status")
        if status not in {"READY", "PARTIAL", "MISSING", "SOURCE_UNAVAILABLE", "WAITING_SOURCES", "UNKNOWN", "BLOCKED_QUALITY"}:
            raise ValueError(f"source capability status is invalid: {capability_id}")
        reason = state.get("reasonCode")
        if (status == "READY" and (state.get("asOf") != as_of or reason is not None)) or (
            status != "READY" and (not isinstance(reason, str) or not reason)
        ):
            raise ValueError(f"source capability readiness evidence is invalid: {capability_id}")
        source_dates = state.get("sourceDates")
        coverage = state.get("coverage")
        if (
            not isinstance(source_dates, dict)
            or any(value is not None and not _is_date(value) for value in source_dates.values())
            or not isinstance(coverage, dict)
            or any(not _is_nonnegative_count(value) for value in coverage.values())
        ):
            raise ValueError(f"source capability evidence shape is invalid: {capability_id}")
        refs = state.get("artifactRefs")
        expected_names = CAPABILITY_ARTIFACTS[capability_id]
        if not isinstance(refs, list) or len(refs) != len(expected_names):
            raise ValueError(f"source capability artifact references are invalid: {capability_id}")
        for ref, name in zip(refs, expected_names, strict=True):
            descriptor = artifacts.get(name)
            if (
                not isinstance(ref, dict)
                or not isinstance(descriptor, dict)
                or ref.get("name") != name
                or ref.get("key") != descriptor.get("key")
                or ref.get("sha256") != descriptor.get("sha256")
                or ref.get("bytes") != descriptor.get("bytes")
            ):
                raise ValueError(f"source capability artifact reference mismatch: {capability_id}:{name}")
        if capability_id == "etf_periods" and not _period_contract_is_consistent(state):
            raise ValueError("source etf_periods evidence is inconsistent")

    core = capabilities["etf_daily_core"]
    if core["status"] == "READY":
        dates = core.get("sourceDates")
        coverage = core.get("coverage")
        if (
            not isinstance(dates, dict)
            or dates.get("snapshot") != as_of
            or dates.get("sseEtfAsOf") != as_of
            or dates.get("szseEtfAsOf") != as_of
            or not isinstance(coverage, dict)
            or not isinstance(coverage.get("instrumentCount"), int)
            or isinstance(coverage.get("instrumentCount"), bool)
            or coverage["instrumentCount"] <= 0
            or not isinstance(coverage.get("tradingDays"), int)
            or isinstance(coverage.get("tradingDays"), bool)
            or coverage["tradingDays"] < 2
        ):
            raise ValueError("source READY etf_daily_core proof is invalid")
    if any(
        capabilities[name]["status"] == "READY"
        for name in ("etf_radar", "rotation", "national_team", "etf_periods")
    ) and core["status"] != "READY":
        raise ValueError("source READY ETF capability lacks core proof")
    for capability_id in ("etf_radar", "rotation", "national_team"):
        state = capabilities[capability_id]
        if state["status"] != "READY":
            continue
        dates = state.get("sourceDates")
        if (
            not isinstance(dates, dict)
            or dates.get("sseEtfAsOf") != as_of
            or dates.get("szseEtfAsOf") != as_of
            or not _is_nonnegative_count(state.get("coverage", {}).get("sampleCount"))
            or state["coverage"]["sampleCount"] == 0
        ):
            raise ValueError(f"source READY {capability_id} proof is invalid")

    for capability_id, date_key in (("connect", "connectAsOf"), ("margin", "marginAsOf")):
        state = capabilities[capability_id]
        if state["status"] == "READY" and (
            state["sourceDates"].get(date_key) != as_of
            or not _is_nonnegative_count(state["coverage"].get("recordCount"))
            or state["coverage"]["recordCount"] == 0
        ):
            raise ValueError(f"source READY {capability_id} proof is invalid")

    penetration = capabilities["etf_penetration"]
    if penetration["status"] == "READY":
        dates = penetration.get("sourceDates")
        coverage = penetration.get("coverage")
        if (
            not isinstance(dates, dict)
            or dates.get("pcfEffectiveAsOf") != as_of
            or not _is_date(dates.get("pcfAsOf"))
            or not isinstance(coverage, dict)
            or not isinstance(coverage.get("stockCount"), int)
            or isinstance(coverage.get("stockCount"), bool)
            or coverage["stockCount"] <= 0
            or not isinstance(coverage.get("matchedEtfCount"), int)
            or isinstance(coverage.get("matchedEtfCount"), bool)
            or coverage["matchedEtfCount"] <= 0
        ):
            raise ValueError("source READY etf_penetration proof is invalid")
    if any(capabilities[name]["status"] == "READY" for name in ("stock_search", "stock_detail")) and penetration["status"] != "READY":
        raise ValueError("source READY stock capability lacks penetration proof")
    if penetration["status"] == "READY":
        penetration_dates = penetration["sourceDates"]
        for capability_id in ("stock_search", "stock_detail"):
            state = capabilities[capability_id]
            if state["status"] != "READY":
                continue
            dates = state["sourceDates"]
            if (
                dates.get("pcfEffectiveAsOf") != as_of
                or dates.get("pcfAsOf") != penetration_dates.get("pcfAsOf")
                or dates.get("pcfEffectiveAsOf") != penetration_dates.get("pcfEffectiveAsOf")
                or not _is_nonnegative_count(state["coverage"].get("stockCount"))
                or state["coverage"]["stockCount"] == 0
            ):
                raise ValueError(f"source READY {capability_id} proof is invalid")


__all__ = [
    "CAPABILITY_ARTIFACTS",
    "CAPABILITY_CONTRACT_VERSION",
    "CAPABILITY_IDS",
    "CAPABILITY_MANIFEST_SCHEMA_VERSION",
    "CAPABILITY_SLOT",
    "ETF_PERIOD_IDS",
    "ETF_PERIOD_METRICS",
    "bind_capability_artifact_refs",
    "build_capability_states",
    "capability_contract_identity",
    "states_from_capability_contract",
    "validate_capability_contract_for_split",
]
