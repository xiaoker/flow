from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .etf_classification import CLASSIFICATION_VERSION, classify_etf
from .etf_facts import latest_etf_flows_from_terminal


STATE_VERSION = 1
MAX_TRADING_DAYS = 260
CALCULATION_METHOD = "sum_of_daily_official_share_changes"
CASH_CALCULATION_METHOD = "sum_of_daily_share_changes_times_same_day_nav"
PERIOD_LOOKBACKS: dict[str, int] = {
    "1d": 1,
    "3d": 3,
    "1w": 5,
    "2w": 10,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "12m": 252,
}
PERIOD_ORDER = ("1d", "3d", "1w", "2w", "1m", "3m", "6m", "ytd", "12m")


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
                    "cashValues": [None] * len(dates),
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
            cash_value = (raw_item.get("estimatedCashPeriods") or {}).get("1d")
            try:
                cash_value = float(cash_value) if cash_value is not None else None
            except (TypeError, ValueError):
                cash_value = None
            row["cashValues"][date_index[trade_date]] = (
                round(cash_value, 2)
                if cash_value is not None and math.isfinite(cash_value)
                else None
            )

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
        "cashCalculationMethod": CASH_CALCULATION_METHOD,
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


def materialize_etf_terminal(
    state: dict[str, Any],
    *,
    base_terminal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize the dashboard ETF view from the offline rolling state.

    This is deliberately a pure function. It is used by the collector/build
    job, never by a Worker request. The resulting terminal contains all
    period values, daily series, group totals, coverage and history metadata
    needed by the UI, so the serving path only streams a published artifact.
    """

    dates = [str(value) for value in state.get("dates") or []]
    if not dates:
        raise ValueError("ETF state has no dates")
    state_rows = [item for item in state.get("instruments") or [] if isinstance(item, dict)]
    base_by_key = {
        f"{item.get('market', 'SSE')}:{str(item.get('code', '')).zfill(6)}": item
        for item in (base_terminal or {}).get("instruments") or []
        if isinstance(item, dict)
    }

    def key_for(item: dict[str, Any]) -> str:
        return f"{item.get('market', 'SSE')}:{str(item.get('code', '')).zfill(6)}"

    def value_at(item: dict[str, Any], field: str, index: int) -> float | None:
        values = item.get(field) or []
        if index >= len(values) or values[index] is None:
            return None
        try:
            number = float(values[index])
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    latest_index = len(dates) - 1

    available_periods: list[str] = []
    anchors: dict[str, int] = {}
    for period, offset in PERIOD_LOOKBACKS.items():
        if len(dates) > offset:
            available_periods.append(period)
            anchors[period] = offset
    current_year = dates[-1][:4]
    prior_year_indices = [index for index, date in enumerate(dates) if date[:4] < current_year]
    if prior_year_indices:
        available_periods.insert(-1 if "12m" in available_periods else len(available_periods), "ytd")

    instruments: list[dict[str, Any]] = []
    for state_item in state_rows:
        # The rolling state intentionally keeps instruments that were present
        # on an earlier official day so their historical series remain
        # reproducible.  They are not part of the current display universe,
        # however, when the latest official snapshot has no row for them.
        # Excluding these inactive rows prevents stale metadata from failing
        # raw-to-terminal identity QA and avoids showing obsolete ETFs as if
        # they still traded.
        if value_at(state_item, "values", latest_index) is None:
            continue
        key = key_for(state_item)
        original = base_by_key.get(key, {})
        merged = {**original, **{
            "code": str(state_item.get("code") or original.get("code") or "").zfill(6),
            "name": str(state_item.get("name") or original.get("name") or state_item.get("code") or ""),
            "market": state_item.get("market") or original.get("market") or "SSE",
            "category": state_item.get("category") or original.get("category") or "未归类",
            "groupType": state_item.get("groupType") or original.get("groupType") or "unclassified",
            "assetClass": state_item.get("assetClass") or original.get("assetClass"),
            "classificationSource": state_item.get("classificationSource") or original.get("classificationSource"),
            "classificationConfidence": state_item.get("classificationConfidence") or original.get("classificationConfidence"),
            "classificationRule": state_item.get("classificationRule") or original.get("classificationRule"),
            "includeInGroup": True,
        }}
        periods: dict[str, float | None] = {}
        period_coverage: dict[str, int] = {}
        cash_periods: dict[str, float | None] = {}
        cash_coverage: dict[str, int] = {}
        for period in available_periods:
            if period == "ytd":
                indices = [index for index, date in enumerate(dates) if date[:4] == current_year]
            else:
                indices = list(range(max(0, len(dates) - anchors[period]), len(dates)))
            values = [value_at(state_item, "values", index) for index in indices]
            observed = [value for value in values if value is not None]
            period_coverage[period] = len(observed)
            periods[period] = round(sum(observed), 6) if observed and len(observed) == len(indices) else None
            cash_values = [value_at(state_item, "cashValues", index) for index in indices]
            cash_observed = [value for value in cash_values if value is not None]
            cash_coverage[period] = len(cash_observed)
            cash_periods[period] = round(sum(cash_observed), 2) if cash_observed and len(cash_observed) == len(indices) else None
        values = [value_at(state_item, "values", index) for index in range(len(dates))]
        merged["periods"] = periods
        merged["periodCoverage"] = period_coverage
        merged["estimatedCashPeriods"] = cash_periods
        merged["cashPeriodCoverage"] = cash_coverage
        merged["coverage"] = sum(value is not None for value in values)
        instruments.append(merged)

    instruments.sort(key=lambda item: abs(item.get("periods", {}).get("1d") or 0), reverse=True)

    daily_series: list[dict[str, Any]] = []
    for index, trade_date in enumerate(dates):
        share_values = [value_at(item, "values", index) for item in state_rows]
        share_observed = [value for value in share_values if value is not None]
        cash_values = [value_at(item, "cashValues", index) for item in state_rows]
        cash_observed = [value for value in cash_values if value is not None]
        daily_series.append({
            "date": trade_date[5:],
            "value": round(sum(share_observed), 6),
            "coverage": len(share_observed),
            "cashValue": round(sum(cash_observed), 2) if cash_observed else None,
            "cashCoverage": len(cash_observed),
        })

    groups: dict[str, dict[str, Any]] = {}
    for item in instruments:
        category = str(item.get("category") or "未归类")
        aggregate = groups.setdefault(category, {
            "groupType": item.get("groupType") or "unclassified",
            "periods": {period: 0.0 for period in available_periods},
            "estimatedCashPeriods": {period: 0.0 for period in available_periods},
            "estimatedCashCoverage": {period: 0 for period in available_periods},
            "componentCount": 0,
            "comparableCount": {period: 0 for period in available_periods},
        })
        aggregate["componentCount"] += 1
        for period in available_periods:
            value = item["periods"].get(period)
            if value is not None:
                aggregate["periods"][period] += value
                aggregate["comparableCount"][period] += 1
            cash_value = item["estimatedCashPeriods"].get(period)
            if cash_value is not None:
                aggregate["estimatedCashPeriods"][period] += cash_value
            aggregate["estimatedCashCoverage"][period] += item["cashPeriodCoverage"].get(period, 0)
    group_periods = [
        {
            "name": name,
            "groupType": aggregate["groupType"],
            "periods": {period: round(value, 6) if aggregate["comparableCount"][period] else None for period, value in aggregate["periods"].items()},
            "estimatedCashPeriods": {period: round(value, 2) if aggregate["estimatedCashCoverage"][period] else None for period, value in aggregate["estimatedCashPeriods"].items()},
            "estimatedCashCoverage": aggregate["estimatedCashCoverage"],
            "componentCount": aggregate["componentCount"],
            "comparableCount": aggregate["comparableCount"],
        }
        for name, aggregate in groups.items()
    ]
    group_periods.sort(key=lambda item: abs(item["periods"].get("1d") or 0), reverse=True)

    market_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for item in instruments:
        market = str(item.get("market") or "SSE")
        category = str(item.get("category") or "未归类")
        market_counts[market] = market_counts.get(market, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
    cash_eligible = sum(item["estimatedCashPeriods"].get("1d") is not None for item in instruments)
    previous_terminal = base_terminal or {}
    return {
        "scope": previous_terminal.get("scope", "SSE+SZSE"),
        "dailySeries": daily_series,
        "instruments": instruments,
        "groupPeriods": group_periods,
        "availablePeriods": [period for period in PERIOD_ORDER if period in available_periods],
        "classification": previous_terminal.get("classification", "官方产品/投资类型优先，低置信度产品统一进入‘未归类’"),
        "classificationVersion": state.get("classificationVersion") or previous_terminal.get("classificationVersion"),
        "calculation": "离线汇总官方逐日份额变化；金额估算按逐日份额变化 × 对应交易日官方单位净值累计",
        "cashFlow": {
            "available": cash_eligible > 0,
            "unit": "亿元",
            "method": CASH_CALCULATION_METHOD,
            "eligibleInstruments": cash_eligible,
            "totalInstruments": len(instruments),
            "coverageDate": dates[-1],
            "warning": "缺少对应日期官方单位净值的 ETF 不计入金额汇总",
        },
        "historyCoverage": {
            "from": dates[0],
            "to": dates[-1],
            "tradingDays": len(dates),
            "stateVersion": int(state.get("version") or STATE_VERSION),
        },
        "universe": {
            "total": len(instruments),
            "excluded": int((previous_terminal.get("universe") or {}).get("excluded", 0)),
            "exclusionCounts": (previous_terminal.get("universe") or {}).get("exclusionCounts", {}),
            "marketCounts": market_counts,
            "categoryCounts": category_counts,
            "classified": sum(item["category"] != "未归类" for item in instruments),
            "unclassified": sum(item["category"] == "未归类" for item in instruments),
            "periodComparable": sum(item["periods"].get("1d") is not None for item in instruments),
            "cashFlowEligible": cash_eligible,
        },
    }


def _trim_etf_flow_state(state: dict[str, Any], as_of: str) -> dict[str, Any]:
    dates = [str(value) for value in state.get("dates") or []]
    if not dates:
        raise ValueError("ETF state has no dates")
    if as_of not in dates:
        raise ValueError(f"ETF state does not contain requested date: {as_of}")
    target_index = dates.index(as_of)
    if target_index == len(dates) - 1:
        return state
    trimmed = deepcopy(state)
    trimmed["dates"] = dates[: target_index + 1]
    for item in trimmed.get("instruments") or []:
        if not isinstance(item, dict):
            continue
        item["values"] = list(item.get("values") or [])[: target_index + 1]
        item["cashValues"] = list(item.get("cashValues") or [])[: target_index + 1]
    return trimmed


def materialize_dashboard_snapshot(
    snapshot: dict[str, Any],
    state: dict[str, Any],
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Overlay a verified rolling ETF state onto a base dashboard snapshot.

    Collection and cross-market completeness are decided by
    :func:`build_dashboard_snapshot`; this helper only performs the offline
    state materialization after the history importer has persisted the latest
    ETF state.  Keeping this step pure makes the final artifact reproducible
    in release QA and keeps large-state work out of Worker requests.
    """

    target = str(as_of or snapshot.get("asOf") or "")
    if not target:
        raise ValueError("dashboard snapshot has no asOf")
    trimmed_state = _trim_etf_flow_state(state, target)
    result = deepcopy(snapshot)
    result["asOf"] = target
    result["gate"] = "conditional_pass"
    publication = dict(result.get("publication") or {})
    publication.update(
        {
            "status": "confirmed",
            "targetAsOf": target,
            "pendingDomains": [],
            "detail": "ETF 原始份额已补齐并由离线连续官方快照物化；Worker 不再请求时重算",
        }
    )
    result["publication"] = publication
    result["etfTerminal"] = materialize_etf_terminal(
        trimmed_state,
        base_terminal=result.get("etfTerminal") or {},
    )
    # The rolling state is the canonical source for every ETF-facing view.
    # Re-derive the homepage headline/top-five after materialization so the
    # state overlay cannot leave an older partial ``etfFlows`` value behind.
    etf_flows, etf_summary = latest_etf_flows_from_terminal(result["etfTerminal"])
    result["etfFlows"] = etf_flows
    for metric in result.get("metrics") or []:
        if isinstance(metric, dict) and metric.get("id") == "etf":
            metric["value"] = f"{etf_summary['net']:+,.2f}"
            metric["delta"] = (
                f"{etf_summary['increasers']} 只增加 · "
                f"{etf_summary['decreasers']} 只减少"
            )
            metric["tone"] = "positive" if etf_summary["net"] >= 0 else "negative"
            metric["eyebrow"] = "沪深 ETF 份额变化"
            basis = metric.get("basis")
            if isinstance(basis, dict):
                basis["detail"] = "上交所与深交所当日官方份额 − 前一交易日官方份额"
    for row in result.get("dataStatus") or []:
        if isinstance(row, dict) and row.get("name") in {"上交所 ETF", "深交所 ETF"}:
            row["asOf"] = target[5:]
            row["status"] = "ready"
            row["detail"] = "官方原始份额已补齐；ETF 状态由离线连续快照物化"
    return result
