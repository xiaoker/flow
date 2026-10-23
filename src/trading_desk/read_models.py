"""Deterministic, offline read models for the dashboard.

Read models are the serving contract between the collector and the web app.
They are generated after the source/quality gates pass, stored as immutable
objects, and selected through a small release manifest. Nothing in this module
is imported by the request-time Worker path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .stock_pressure import PRESSURE_SCHEMA_VERSION, build_stock_pressure, stock_shard_name
from .read_model_capabilities import (
    CAPABILITY_CONTRACT_VERSION,
    CAPABILITY_MANIFEST_SCHEMA_VERSION,
    bind_capability_artifact_refs,
    build_capability_states,
    capability_contract_identity,
)


SCHEMA_VERSION = 3
VIEW_NAMES = (
    "bootstrap",
    "overview",
    "radar",
    "radar_flow",
    "rotation",
    "national",
    "connect",
    "margin",
    "pressure",
)
STOCK_SEARCH_NAME = "stock_search"
STOCK_SHARD_COUNT = 8
STOCK_SHARD_NAMES = tuple(f"stock_{index}" for index in range(STOCK_SHARD_COUNT))
REQUIRED_ARTIFACT_NAMES = (*VIEW_NAMES, STOCK_SEARCH_NAME, *STOCK_SHARD_NAMES)
MARKET_READ_MODEL_NAMES = (
    "bootstrap",
    "overview",
    "radar",
    "rotation",
    "national",
    "connect",
    "margin",
)
MAX_READ_MODEL_BYTES = 5_000_000
MARKET_HISTORY_MAX_DATES = 750
MARKET_HISTORY_SCHEMA_VERSION = 1
MARKET_HISTORY_SCOPE_VERSION = "sse-szse-bse-v1"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# This is the same code-based observation pool shown by the National Team
# page.  The page is intentionally a projection of ETF Radar, not a second
# source or an independent collection universe.
NATIONAL_TEAM_CODES = (
    "510300",
    "510310",
    "510330",
    "510500",
    "159915",
    "159919",
    "512100",
    "510050",
    "588080",
    "510180",
    "159845",
    "512500",
)


class ReadModelError(ValueError):
    """Raised when a snapshot cannot be safely published as a read model."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON in a stable form so hashes and releases are reproducible."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _observed_at_utc(snapshot: dict[str, Any]) -> str:
    """Normalize the collection timestamp to an unambiguous UTC instant.

    The dashboard snapshot is produced in Asia/Shanghai, while the release
    ordering contract deliberately compares timestamps lexically.  Emitting
    one canonical ``Z`` representation prevents ``+08:00`` and ``Z`` values
    for the same instant from sorting differently.
    """

    raw = snapshot.get("observedAt")
    if not isinstance(raw, str) or not raw.strip():
        raise ReadModelError("observed_at_missing")
    value = raw.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReadModelError("observed_at_invalid") from exc
    if parsed.tzinfo is None:
        raise ReadModelError("observed_at_timezone_missing")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _source_date(value: Any, *, year: int) -> str | None:
    """Turn a full or MM-DD source date into a validated ISO date."""

    if value is None:
        return None
    text = str(value).strip()
    if ISO_DATE.fullmatch(text):
        return text
    if re.fullmatch(r"\d{2}-\d{2}", text):
        candidate = f"{year:04d}-{text}"
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
        except ValueError:
            return None
        return candidate
    return None


def _source_dates(snapshot: dict[str, Any]) -> dict[str, str | None]:
    """Expose dataset source dates without pretending they share one clock.

    ``asOf`` is the release's decision date.  The entries below retain the
    source-date semantics used by the collectors (ETF share date, margin
    trade date, connect trade date, and PCF date) so a consumer can explain
    why an otherwise complete release may contain a next-day publication.
    """

    as_of = str(snapshot["asOf"])
    year = int(as_of[:4])
    dates: dict[str, str | None] = {
        "snapshot": as_of,
        "sseEtfAsOf": None,
        "szseEtfAsOf": None,
        "marginAsOf": None,
        "connectAsOf": None,
    }
    for key, series_key in (("marginAsOf", "marginSeries"), ("connectAsOf", "connectSeries")):
        series = snapshot.get(series_key)
        if isinstance(series, list):
            candidates = [
                _source_date(item.get("date"), year=year)
                for item in series
                if isinstance(item, dict)
            ]
            dates[key] = next((item for item in reversed(candidates) if item), None)
        else:
            dates[key] = None
    stock_flow = snapshot.get("etfStockFlow")
    basis = stock_flow.get("basis") if isinstance(stock_flow, dict) else None
    if isinstance(basis, dict):
        dates["pcfEffectiveAsOf"] = _source_date(basis.get("shareDate"), year=year)
        dates["pcfAsOf"] = _source_date(basis.get("pcfDate"), year=year)
    else:
        dates["pcfEffectiveAsOf"] = as_of
        dates["pcfAsOf"] = None
    for row in snapshot.get("dataStatus") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if name == "上交所 ETF":
            dates["sseEtfAsOf"] = _source_date(row.get("asOf"), year=year)
        elif name == "深交所 ETF":
            dates["szseEtfAsOf"] = _source_date(row.get("asOf"), year=year)
        elif name == "融资融券":
            dates["marginAsOf"] = _source_date(row.get("asOf"), year=year)
        elif name == "互联互通":
            dates["connectAsOf"] = _source_date(row.get("asOf"), year=year)
    return dates


_MARKET_HISTORY_FIELDS = (
    "trade_date",
    "margin_balance",
    "margin_change",
    "financing_buy",
    "lending_balance",
    "lending_sell_volume",
    "total_balance",
    "sse_balance",
    "szse_balance",
    "southbound_net",
    "southbound_turnover",
    "southbound_sse_net",
    "southbound_szse_net",
    "northbound_turnover",
    "northbound_etf_turnover",
    "northbound_trade_count",
    "margin_available",
    "connect_available",
    "source_snapshot_sha256",
    "observed_at",
    "bse_balance",
    "margin_scope_version",
)
_MARKET_HISTORY_NUMERIC_FIELDS = {
    "margin_balance",
    "margin_change",
    "financing_buy",
    "lending_balance",
    "lending_sell_volume",
    "total_balance",
    "sse_balance",
    "szse_balance",
    "bse_balance",
    "southbound_net",
    "southbound_turnover",
    "southbound_sse_net",
    "southbound_szse_net",
    "northbound_turnover",
    "northbound_etf_turnover",
    "northbound_trade_count",
}
MARGIN_SCOPE_V1 = "sse_szse_v1"
MARGIN_SCOPE_V2 = "sse_szse_bse_v2"


def _is_valid_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _finite_or_none(value: Any, *, field: str, date: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReadModelError(f"market_history_{field}_invalid:{date}")
    number = float(value)
    if not math.isfinite(number):
        raise ReadModelError(f"market_history_{field}_non_finite:{date}")
    # Preserve integer flags and avoid introducing binary floating point noise
    # into the compact input hash for values that were already integers.
    return value if isinstance(value, int) else number


def _normalize_market_history(
    market_history: dict[str, Any] | list[dict[str, Any]],
    *,
    as_of: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Validate and bound the compact market-history input.

    The collector and the public read-only endpoint use the same row shape.
    Normalizing it here gives the immutable producer one deterministic input,
    filters future rows before they can enter a historical release, and keeps
    the Worker path free from full-snapshot aggregation.
    """

    if isinstance(market_history, list):
        raw_rows = market_history
        metadata: dict[str, Any] = {}
    elif _is_record(market_history):
        metadata = dict(market_history)
        raw_rows = metadata.get("rows")
        if raw_rows is None:
            # A compact history may be supplied as a date-keyed map by an
            # offline migration.  Accept it only when every value is a row;
            # the persisted producer output remains the canonical rows list.
            candidate_rows = [value for key, value in metadata.items() if _is_valid_iso_date(key)]
            raw_rows = candidate_rows if candidate_rows else None
        declared_as_of = metadata.get("asOf")
        if declared_as_of not in (None, as_of):
            raise ReadModelError("market_history_as_of_mismatch")
        schema_version = metadata.get("schemaVersion")
        if schema_version not in (None, MARKET_HISTORY_SCHEMA_VERSION):
            raise ReadModelError("market_history_schema_invalid")
        scope_version = metadata.get("scopeVersion")
        if scope_version not in (None, "sse-szse-v1", MARKET_HISTORY_SCOPE_VERSION):
            raise ReadModelError("market_history_scope_invalid")
    else:
        raise ReadModelError("market_history_not_object")
    if not isinstance(raw_rows, list):
        raise ReadModelError("market_history_rows_missing")

    normalized: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for raw in raw_rows:
        if not _is_record(raw):
            raise ReadModelError("market_history_row_not_object")
        trade_date = raw.get("trade_date")
        if not _is_valid_iso_date(trade_date):
            raise ReadModelError("market_history_trade_date_invalid")
        # A source endpoint can return a bounded archive that includes rows
        # newer than a requested historical date.  They are intentionally not
        # an error: they are excluded from this release, never backfilled.
        if trade_date > as_of:
            continue
        if trade_date in seen_dates:
            raise ReadModelError(f"market_history_duplicate_trade_date:{trade_date}")
        seen_dates.add(trade_date)
        row: dict[str, Any] = {"trade_date": trade_date}
        for field in _MARKET_HISTORY_NUMERIC_FIELDS:
            row[field] = _finite_or_none(raw.get(field), field=field, date=trade_date)
        for field in ("margin_available", "connect_available"):
            flag = raw.get(field, 0)
            if isinstance(flag, bool):
                flag = int(flag)
            if flag not in (0, 1):
                raise ReadModelError(f"market_history_{field}_invalid:{trade_date}")
            row[field] = flag
        source_hash = raw.get("source_snapshot_sha256", "")
        if source_hash is None:
            source_hash = ""
        if not isinstance(source_hash, str):
            raise ReadModelError(f"market_history_source_hash_invalid:{trade_date}")
        observed_at = raw.get("observed_at", "")
        if observed_at is None:
            observed_at = ""
        if not isinstance(observed_at, str):
            raise ReadModelError(f"market_history_observed_at_invalid:{trade_date}")
        row["source_snapshot_sha256"] = source_hash
        row["observed_at"] = observed_at
        scope = raw.get("margin_scope_version", MARGIN_SCOPE_V1)
        if scope not in (MARGIN_SCOPE_V1, MARGIN_SCOPE_V2):
            raise ReadModelError(f"market_history_margin_scope_invalid:{trade_date}")
        if scope == MARGIN_SCOPE_V2 and row["margin_available"] == 1 and row["bse_balance"] is None:
            raise ReadModelError(f"market_history_bse_balance_missing:{trade_date}")
        row["margin_scope_version"] = scope
        normalized.append(row)

    normalized.sort(key=lambda item: item["trade_date"])
    normalized = normalized[-MARKET_HISTORY_MAX_DATES:]

    def margin_point(row: dict[str, Any]) -> dict[str, Any] | None:
        if row["margin_available"] != 1 or row["margin_balance"] is None:
            return None
        point = {
            "date": row["trade_date"],
            "balance": row["margin_balance"],
            "label": f"{float(row['margin_balance']) / 10_000:.4f} 万亿",
            "change": row["margin_change"],
        }
        for source, target in (
            ("financing_buy", "financingBuy"),
            ("lending_balance", "lendingBalance"),
            ("lending_sell_volume", "lendingSellVolume"),
            ("total_balance", "totalBalance"),
            ("sse_balance", "sseBalance"),
            ("szse_balance", "szseBalance"),
            ("bse_balance", "bseBalance"),
        ):
            if row[source] is not None:
                point[target] = row[source]
        point["marginScopeVersion"] = row["margin_scope_version"]
        return point

    def connect_point(row: dict[str, Any]) -> dict[str, Any] | None:
        if (
            row["connect_available"] != 1
            or row["southbound_net"] is None
            or row["northbound_turnover"] is None
        ):
            return None
        return {
            "date": row["trade_date"],
            "southboundNet": row["southbound_net"],
            "southboundTurnover": row["southbound_turnover"] or 0,
            "southboundSseNet": row["southbound_sse_net"] or 0,
            "southboundSzseNet": row["southbound_szse_net"] or 0,
            "northboundTurnover": row["northbound_turnover"],
            "northboundEtfTurnover": row["northbound_etf_turnover"] or 0,
            "northboundTradeCount": row["northbound_trade_count"] or 0,
        }

    margin_series = [point for row in normalized if (point := margin_point(row)) is not None]
    connect_series = [point for row in normalized if (point := connect_point(row)) is not None]
    coverage = {
        "archiveFrom": normalized[0]["trade_date"] if normalized else None,
        "archiveTo": normalized[-1]["trade_date"] if normalized else None,
        "archiveDates": len(normalized),
        "margin": {
            "from": margin_series[0]["date"] if margin_series else None,
            "to": margin_series[-1]["date"] if margin_series else None,
            "points": len(margin_series),
        },
        "connect": {
            "from": connect_series[0]["date"] if connect_series else None,
            "to": connect_series[-1]["date"] if connect_series else None,
            "points": len(connect_series),
        },
    }
    compact = {
        "schemaVersion": MARKET_HISTORY_SCHEMA_VERSION,
        "scopeVersion": MARKET_HISTORY_SCOPE_VERSION,
        "asOf": as_of,
        "source": metadata.get("source", "market_history_daily"),
        "rows": normalized,
        "coverage": coverage,
    }
    return compact, margin_series, connect_series, coverage


def _market_history_contract(compact: dict[str, Any] | None) -> dict[str, Any]:
    if compact is None:
        return {
            "schemaVersion": MARKET_HISTORY_SCHEMA_VERSION,
            "scopeVersion": MARKET_HISTORY_SCOPE_VERSION,
            "sha256": None,
            "archiveFrom": None,
            "archiveTo": None,
            "archiveDates": 0,
            "margin": {"from": None, "to": None, "points": 0},
            "connect": {"from": None, "to": None, "points": 0},
        }
    coverage = compact["coverage"]
    return {
        "schemaVersion": MARKET_HISTORY_SCHEMA_VERSION,
        "scopeVersion": compact.get("scopeVersion", MARKET_HISTORY_SCOPE_VERSION),
        "sha256": sha256_bytes(canonical_json_bytes(compact)),
        **coverage,
    }


def _assert_history_not_regressed(
    current: dict[str, Any],
    previous_manifest: dict[str, Any] | None,
) -> None:
    """Fail closed when a new release loses confirmed compact history."""

    if not isinstance(previous_manifest, dict):
        return
    nested = previous_manifest.get("manifest")
    previous = nested if isinstance(nested, dict) else previous_manifest
    previous_history = previous.get("marketHistory")
    if not isinstance(previous_history, dict):
        return
    for domain in ("margin", "connect"):
        previous_domain = previous_history.get(domain)
        current_domain = current.get(domain)
        if not isinstance(previous_domain, dict) or not isinstance(current_domain, dict):
            continue
        previous_points = previous_domain.get("points")
        current_points = current_domain.get("points")
        if isinstance(previous_points, int) and isinstance(current_points, int) and current_points < previous_points:
            raise ReadModelError(
                f"market_history_{domain}_regressed:{current_points}<{previous_points}"
            )


def _read_model_coverage(
    snapshot: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return source-derived coverage counters for the immutable release."""

    terminal = snapshot.get("etfTerminal")
    terminal = terminal if isinstance(terminal, dict) else {}
    history = terminal.get("historyCoverage")
    history = history if isinstance(history, dict) else {}
    pressure_envelope = payloads.get("pressure") or {}
    pressure_data = pressure_envelope.get("data")
    pressure_data = pressure_data if isinstance(pressure_data, dict) else {}
    pressure = pressure_data.get("pressure")
    pressure = pressure if isinstance(pressure, dict) else {}
    pressure_coverage = pressure.get("coverage")
    pressure_coverage = pressure_coverage if isinstance(pressure_coverage, dict) else {}
    available_dates = pressure.get("availableDates")
    if not isinstance(available_dates, list):
        available_dates = []
    search = payloads.get(STOCK_SEARCH_NAME) or {}
    search_items = search.get("items") if isinstance(search.get("items"), list) else []
    national = payloads.get("national") or {}
    national_data = national.get("data") if isinstance(national.get("data"), dict) else {}
    national_terminal = national_data.get("etfTerminal")
    national_terminal = national_terminal if isinstance(national_terminal, dict) else {}
    national_items = national_terminal.get("instruments")
    national_items = national_items if isinstance(national_items, list) else []
    periods = terminal.get("availablePeriods")
    periods = periods if isinstance(periods, list) else []
    return {
        "etf": {
            "tradingDays": int(history.get("tradingDays") or 0),
            "instrumentCount": len(terminal.get("instruments") or []),
            "availablePeriods": sorted(str(period) for period in periods),
        },
        "pressure": {
            "availableDateCount": len(available_dates),
            "stockCount": int(pressure_coverage.get("stockCount") or 0),
            "history20StockCount": int(pressure_coverage.get("history20StockCount") or 0),
        },
        "stockSearch": {"count": len(search_items)},
        "national": {"count": len(national_items)},
    }


def _assert_read_model_coverage_not_regressed(
    current: dict[str, Any],
    previous_manifest: dict[str, Any] | None,
) -> None:
    """Reject a candidate that silently loses historical coverage."""

    if not isinstance(previous_manifest, dict):
        return
    nested = previous_manifest.get("manifest")
    previous = nested if isinstance(nested, dict) else previous_manifest
    old = previous.get("coverage")
    if not isinstance(old, dict):
        return
    # history20StockCount is a current-stock cohort, not historical coverage:
    # ETF basket rebalances can legitimately remove a stock from that cohort.
    for section, field in (
        ("etf", "tradingDays"),
        ("pressure", "availableDateCount"),
    ):
        old_section = old.get(section)
        new_section = current.get(section)
        if not isinstance(old_section, dict) or not isinstance(new_section, dict):
            continue
        old_value = old_section.get(field)
        new_value = new_section.get(field)
        if isinstance(old_value, int) and isinstance(new_value, int) and new_value < old_value:
            raise ReadModelError(
                f"read_model_coverage_{section}_{field}_regressed:{new_value}<{old_value}"
            )
    old_periods = (old.get("etf") or {}).get("availablePeriods")
    new_periods = (current.get("etf") or {}).get("availablePeriods")
    if isinstance(old_periods, list) and isinstance(new_periods, list):
        missing = sorted(set(map(str, old_periods)) - set(map(str, new_periods)))
        if missing:
            raise ReadModelError(
                "read_model_coverage_etf_available_periods_regressed:" + ",".join(missing)
            )


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _assert_finite(value: Any, path: str = "$", *, max_depth: int = 12) -> None:
    """Reject NaN/Infinity before an object is made publishable."""

    if max_depth < 0:
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ReadModelError(f"non_finite_value:{path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_finite(item, f"{path}.{key}", max_depth=max_depth - 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite(item, f"{path}[{index}]", max_depth=max_depth - 1)


def validate_snapshot(snapshot: dict[str, Any], *, require_materialized: bool = False) -> None:
    if not _is_record(snapshot):
        raise ReadModelError("snapshot_not_object")
    as_of = snapshot.get("asOf")
    if not isinstance(as_of, str) or not ISO_DATE.fullmatch(as_of):
        raise ReadModelError("snapshot_as_of_invalid")
    quality = snapshot.get("quality")
    if not _is_record(quality):
        raise ReadModelError("snapshot_quality_missing")
    if int(quality.get("fail", 1)) != 0:
        raise ReadModelError("snapshot_quality_failed")
    if snapshot.get("gate") == "blocked":
        raise ReadModelError("snapshot_gate_blocked")

    terminal = snapshot.get("etfTerminal")
    if not _is_record(terminal):
        raise ReadModelError("snapshot_etf_terminal_missing")
    for field in ("instruments", "dailySeries", "groupPeriods"):
        if not isinstance(terminal.get(field), list):
            raise ReadModelError(f"snapshot_etf_terminal_{field}_missing")
    coverage = terminal.get("historyCoverage")
    if require_materialized:
        if not _is_record(coverage):
            raise ReadModelError("etf_history_coverage_missing")
        if coverage.get("to") != as_of:
            raise ReadModelError("etf_history_coverage_as_of_mismatch")
        if not isinstance(coverage.get("tradingDays"), int) or coverage["tradingDays"] < 2:
            raise ReadModelError("etf_history_coverage_incomplete")
        if not isinstance(coverage.get("stateVersion"), int) or coverage["stateVersion"] <= 0:
            raise ReadModelError("etf_history_state_version_missing")
    stock_flow = snapshot.get("etfStockFlow")
    if stock_flow is not None:
        if not isinstance(stock_flow, dict):
            raise ReadModelError("etf_stock_flow_not_object")
        basis = stock_flow.get("basis")
        if not isinstance(basis, dict):
            raise ReadModelError("etf_stock_flow_basis_missing")
        if stock_flow.get("available") is True:
            pcf_alignment = basis.get("pcfAlignment")
            pcf_date = basis.get("pcfDate")
            valid_pcf_alignment = (
                pcf_date == as_of
                or (
                    pcf_alignment == "next_day_previous_trade_date"
                    and isinstance(pcf_date, str)
                    and ISO_DATE.fullmatch(pcf_date)
                    and pcf_date > as_of
                )
            )
            if basis.get("shareDate") != as_of or not valid_pcf_alignment:
                raise ReadModelError("etf_stock_flow_date_mismatch")
            coverage = stock_flow.get("coverage")
            if not isinstance(coverage, dict) or not isinstance(stock_flow.get("stocks"), list):
                raise ReadModelError("etf_stock_flow_coverage_missing")
    _assert_finite(snapshot)


def _with_release_meta(snapshot: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "asOf": snapshot["asOf"],
        "observedAt": snapshot.get("observedAt"),
        "releaseStatus": (snapshot.get("publication") or {}).get("status", "confirmed"),
        "data": payload,
    }


def _bootstrap_terminal(terminal: dict[str, Any]) -> dict[str, Any]:
    """Return only terminal fields used by the default overview.

    Keeping this allowlist explicit prevents a newly-added analytics field (or
    a future embedded graph) from silently turning ``bootstrap.json`` back
    into a full snapshot.  Radar receives the same ETF universe separately;
    the expensive ETF-to-stock graph is never part of either payload.
    """

    allowed = (
        "scope",
        "dailySeries",
        "instruments",
        "groupPeriods",
        "availablePeriods",
        "classification",
        "classificationVersion",
        "calculation",
        "cashFlow",
        "historyCoverage",
        "windowEvidence",
        "universe",
    )
    result = {key: terminal[key] for key in allowed if key in terminal}
    # The default page does not need the Radar period matrix. Keep an empty
    # stable array for consumers that expect the terminal shape; the searchable
    # ETF identity index is published as the sibling ``etfSearch`` field.
    result["instruments"] = []
    result["reconciliation"] = _overview_reconciliation(terminal)
    result["cashAvailablePeriods"] = [
        period
        for period in terminal.get("availablePeriods", [])
        if any(
            isinstance(item, dict)
            and isinstance(item.get("estimatedCashPeriods"), dict)
            and isinstance(item["estimatedCashPeriods"].get(period), (int, float))
            for item in terminal.get("instruments", [])
        )
    ]
    return result


def _etf_search_index(terminal: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a small ETF name/code index for the default global search."""

    items = []
    for item in terminal.get("instruments", []):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if not code or not name:
            continue
        items.append({
            "code": code,
            "name": name,
            "market": item.get("market"),
            "category": item.get("category"),
        })
    return sorted(items, key=lambda item: (str(item["name"]), str(item["code"])))


def _overview_reconciliation(terminal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Precompute overview counters while the producer still has full rows."""

    instruments = [item for item in terminal.get("instruments", []) if isinstance(item, dict)]
    groups = [item for item in terminal.get("groupPeriods", []) if isinstance(item, dict)]
    result: dict[str, dict[str, Any]] = {}
    for period in terminal.get("availablePeriods", []):
        period_result: dict[str, Any] = {}
        for metric, instrument_key, group_key in (
            ("shares", "periods", "periods"),
            ("cash", "estimatedCashPeriods", "estimatedCashPeriods"),
        ):
            comparable = [
                item for item in instruments
                if isinstance(item.get(instrument_key), dict)
                and isinstance(item[instrument_key].get(period), (int, float))
            ]
            values = [float(item[instrument_key][period]) for item in comparable]
            group_values = [
                float(item[group_key][period]) for item in groups
                if isinstance(item.get(group_key), dict)
                and isinstance(item[group_key].get(period), (int, float))
            ]
            period_result[metric] = {
                "comparable": len(comparable),
                "increased": sum(value > 0 for value in values),
                "decreased": sum(value < 0 for value in values),
                "unchanged": sum(value == 0 for value in values),
                "classified": sum(item.get("category") != "未归类" for item in instruments),
                "unclassified": sum(item.get("category") == "未归类" for item in instruments),
                "instrumentTotal": round(sum(values), 2),
                "groupTotal": round(sum(group_values), 2),
                "delta": round(sum(group_values) - sum(values), 2),
            }
        result[str(period)] = period_result
    return result


def _national_terminal(terminal: dict[str, Any]) -> dict[str, Any]:
    """Build the code-based National Team projection from ETF Radar rows."""

    instruments = [
        item
        for item in terminal.get("instruments", [])
        if isinstance(item, dict) and str(item.get("code", "")) in NATIONAL_TEAM_CODES
    ]
    universe = terminal.get("universe") if isinstance(terminal.get("universe"), dict) else {}
    market_counts = {
        "SSE": sum(item.get("market") == "SSE" for item in instruments),
        "SZSE": sum(item.get("market") == "SZSE" for item in instruments),
    }
    category_counts: dict[str, int] = {}
    for item in instruments:
        category = str(item.get("category", "未归类"))
        category_counts[category] = category_counts.get(category, 0) + 1
    projected_universe = {
        "total": len(instruments),
        "excluded": 0,
        "exclusionCounts": {},
        "marketCounts": market_counts,
        "categoryCounts": category_counts,
        "classified": sum(item.get("category") != "未归类" for item in instruments),
        "unclassified": sum(item.get("category") == "未归类" for item in instruments),
        "periodComparable": sum(item.get("periods", {}).get("1d") is not None for item in instruments),
        "cashFlowEligible": sum(
            item.get("estimatedCashPeriods", {}).get("1d") is not None for item in instruments
        ),
    }
    # Preserve future terminal metadata only when it is scalar/structural and
    # cannot re-introduce the full ETF universe or daily history.
    projected = {
        "scope": terminal.get("scope"),
        "dailySeries": [],
        "instruments": instruments,
        "groupPeriods": [],
        "availablePeriods": terminal.get("availablePeriods", []),
        "classification": terminal.get("classification"),
        "classificationVersion": terminal.get("classificationVersion"),
        "calculation": terminal.get("calculation"),
        "cashFlow": terminal.get("cashFlow"),
        "historyCoverage": terminal.get("historyCoverage"),
        "universe": projected_universe,
    }
    if "windowEvidence" in terminal:
        projected["windowEvidence"] = terminal["windowEvidence"]
    # ``universe`` is deliberately recomputed above; do not copy unrelated
    # fields from the full terminal into this child view.
    _ = universe
    return projected


def _market_financing_balance(snapshot: dict[str, Any]) -> float | None:
    """Return the latest official market financing balance in yuan.

    ``dashboard.marginSeries.balance`` is a presentation value in 100-million
    yuan.  Pressure keeps the canonical unit as yuan and never derives a
    stock-level value from this market aggregate.
    """

    series = snapshot.get("marginSeries")
    if not isinstance(series, list) or not series:
        return None
    latest = series[-1]
    if not isinstance(latest, dict):
        return None
    value = latest.get("balance")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number * 100_000_000, 2)


def _build_stock_artifacts(
    stock_catalog: dict[str, Any],
    *,
    as_of: str,
) -> dict[str, dict[str, Any]]:
    """Build one compact search object and deterministic stock detail shards."""

    if not _is_record(stock_catalog):
        raise ReadModelError("stock_catalog_not_object")
    if stock_catalog.get("schemaVersion") != 1:
        raise ReadModelError("stock_catalog_schema_invalid")
    if stock_catalog.get("asOf") != as_of:
        raise ReadModelError("stock_catalog_as_of_mismatch")
    items = stock_catalog.get("items")
    if not isinstance(items, list):
        raise ReadModelError("stock_catalog_items_missing")
    basis = stock_catalog.get("basis")
    if not _is_record(basis):
        raise ReadModelError("stock_catalog_basis_missing")

    search_items: list[dict[str, Any]] = []
    shards: dict[str, list[dict[str, Any]]] = {name: [] for name in STOCK_SHARD_NAMES}
    identities: set[str] = set()
    for item in items:
        if not _is_record(item):
            raise ReadModelError("stock_catalog_item_not_object")
        listing_market = str(item.get("listingMarket", "")).strip()
        stock_code = str(item.get("stockCode", "")).strip()
        stock_name = str(item.get("stockName", "")).strip()
        if not listing_market or not stock_code or not stock_name:
            raise ReadModelError("stock_catalog_identity_missing")
        identity = f"{listing_market}:{stock_code}"
        if identity in identities:
            raise ReadModelError("stock_catalog_duplicate_identity")
        identities.add(identity)
        shard = stock_shard_name(listing_market, stock_code)
        detail = dict(item)
        detail["shard"] = shard
        detail["estimated"] = True
        shards[shard].append(detail)
        search_items.append(
            {
                "listingMarket": listing_market,
                "listingMarketName": str(item.get("listingMarketName", "未知市场")),
                "code": stock_code,
                "name": stock_name,
                "shard": shard,
                "available": True,
            }
        )

    search_items.sort(key=lambda item: (item["name"], item["code"], item["listingMarket"]))
    search_payload = {
        "schemaVersion": 1,
        "asOf": as_of,
        "basis": basis,
        "coverage": stock_catalog.get("coverage", {}),
        "items": search_items,
        "warnings": stock_catalog.get("warnings", []),
    }
    artifacts: dict[str, dict[str, Any]] = {STOCK_SEARCH_NAME: search_payload}
    for shard_name in STOCK_SHARD_NAMES:
        shards[shard_name].sort(
            key=lambda item: (str(item.get("listingMarket", "")), str(item.get("stockCode", "")))
        )
        artifacts[shard_name] = {
            "schemaVersion": 1,
            "asOf": as_of,
            "basis": basis,
            "coverage": stock_catalog.get("coverage", {}),
            "items": shards[shard_name],
            "warnings": stock_catalog.get("warnings", []),
        }
    for name, payload in artifacts.items():
        encoded = canonical_json_bytes(payload)
        if len(encoded) > MAX_READ_MODEL_BYTES:
            raise ReadModelError(f"stock_artifact_too_large:{name}")
        _assert_finite(payload)
    return artifacts


def build_view_payloads(
    snapshot: dict[str, Any],
    *,
    market_history: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Split one validated snapshot into bounded, view-specific payloads."""

    effective_snapshot = snapshot
    if market_history is not None:
        _compact, margin_series, connect_series, coverage = _normalize_market_history(
            market_history,
            as_of=str(snapshot["asOf"]),
        )
        effective_snapshot = dict(snapshot)
        # An explicit history input is authoritative. Empty domain series are
        # preserved as empty instead of falling back to the current collector
        # window and presenting it as long history.
        effective_snapshot["marginSeries"] = margin_series
        effective_snapshot["connectSeries"] = connect_series
        effective_snapshot["marketHistoryCoverage"] = coverage

    terminal = effective_snapshot["etfTerminal"]
    bootstrap = {
        key: value
        for key, value in effective_snapshot.items()
        if key not in {"etfStockFlow", "pressure"}
    }
    bootstrap["etfTerminal"] = _bootstrap_terminal(terminal)
    bootstrap["etfSearch"] = _etf_search_index(terminal)

    # The penetration model is only needed by ETF Radar detail drawers. Keep
    # it out of both the default payload and the list view; the dedicated
    # radar_flow object is fetched only after a drawer is opened.
    overview = {
        key: value
        for key, value in effective_snapshot.items()
        if key not in {"etfTerminal", "etfStockFlow"}
    }
    radar = {
        "etfTerminal": terminal,
        "classificationVersion": terminal.get("classificationVersion"),
    }
    radar_flow = {
        "etfStockFlow": effective_snapshot.get("etfStockFlow"),
        "classificationVersion": terminal.get("classificationVersion"),
    }
    rotation = {
        "dailySeries": terminal.get("dailySeries", []),
        "groupPeriods": terminal.get("groupPeriods", []),
        "availablePeriods": terminal.get("availablePeriods", []),
        "cashFlow": terminal.get("cashFlow"),
        "classification": terminal.get("classification"),
        "classificationVersion": terminal.get("classificationVersion"),
        "calculation": terminal.get("calculation"),
        "historyCoverage": terminal.get("historyCoverage"),
        "universe": terminal.get("universe"),
    }
    if "windowEvidence" in terminal:
        rotation["windowEvidence"] = terminal["windowEvidence"]
    connect = {
        "connect": effective_snapshot.get("connect", []),
        "connectSeries": effective_snapshot.get("connectSeries", []),
        "marketHistoryCoverage": effective_snapshot.get("marketHistoryCoverage"),
        "dataStatus": [row for row in effective_snapshot.get("dataStatus", []) if "互联" in str(row.get("name", ""))],
    }
    margin = {
        "marginSeries": effective_snapshot.get("marginSeries", []),
        "marketHistoryCoverage": effective_snapshot.get("marketHistoryCoverage"),
        "dataStatus": [row for row in effective_snapshot.get("dataStatus", []) if "融资" in str(row.get("name", ""))],
    }
    national = {
        "etfTerminal": _national_terminal(terminal),
        "asOf": snapshot["asOf"],
        "classificationVersion": terminal.get("classificationVersion"),
    }
    return {
        "bootstrap": bootstrap,
        "overview": _with_release_meta(effective_snapshot, overview),
        "radar": _with_release_meta(effective_snapshot, radar),
        "radar_flow": _with_release_meta(effective_snapshot, radar_flow),
        "rotation": _with_release_meta(effective_snapshot, rotation),
        "national": _with_release_meta(effective_snapshot, national),
        "connect": _with_release_meta(effective_snapshot, connect),
        "margin": _with_release_meta(effective_snapshot, margin),
    }


def build_read_models(
    snapshot: dict[str, Any],
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    stock_catalog: dict[str, Any] | None = None,
    market_history: dict[str, Any] | list[dict[str, Any]] | None = None,
    previous_manifest: dict[str, Any] | None = None,
    producer_build_sha: str | None = None,
    capability_contract_version: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return view payloads and a release manifest without touching storage."""

    if "etfReplayInput" in snapshot:
        raise ReadModelError("etf_replay_core_slot_required")
    if "windowEvidence" in (snapshot.get("etfTerminal") or {}) and capability_contract_version != CAPABILITY_CONTRACT_VERSION:
        raise ReadModelError("etf_replay_candidate_contract_required")
    validate_snapshot(snapshot, require_materialized=require_materialized)
    stock_flow = snapshot.get("etfStockFlow")
    if isinstance(stock_flow, dict) and stock_flow.get("available") is True and stock_catalog is None:
        # The radar payload contains only a bounded compatibility graph.  Once
        # the penetration model is available, publishing without its complete
        # search/shard sidecars creates a release that can show clickable
        # stocks but cannot resolve them on the detail page.
        raise ReadModelError("stock_catalog_required_for_available_flow")
    if stock_catalog is None:
        # A v3 release is the serving contract for the complete application,
        # not a partial dashboard snapshot.  Publishing a seven-view manifest
        # would pass local hashing but be rejected by the Worker/history
        # picker after the raw evidence had already been archived.
        raise ReadModelError("stock_catalog_required_for_v3")
    compact_history = None
    effective_snapshot = snapshot
    if market_history is not None:
        compact_history, margin_series, connect_series, coverage = _normalize_market_history(
            market_history,
            as_of=str(snapshot["asOf"]),
        )
        effective_snapshot = dict(snapshot)
        effective_snapshot["marginSeries"] = margin_series
        effective_snapshot["connectSeries"] = connect_series
        effective_snapshot["marketHistoryCoverage"] = coverage
    market_history_contract = _market_history_contract(compact_history)
    _assert_history_not_regressed(market_history_contract, previous_manifest)
    payloads = build_view_payloads(effective_snapshot)
    payloads.update(_build_stock_artifacts(stock_catalog, as_of=snapshot["asOf"]))
    # Keep the pressure page as a compact projection. Full contributor lists
    # stay in the existing stock shards and are fetched only after a user
    # selects one stock.
    pressure = build_stock_pressure(
        stock_catalog,
        market_financing_balance_yuan=_market_financing_balance(effective_snapshot),
        margin_as_of=snapshot["asOf"],
    )
    payloads["pressure"] = _with_release_meta(effective_snapshot, {"pressure": pressure})
    coverage_contract = _read_model_coverage(effective_snapshot, payloads)
    _assert_read_model_coverage_not_regressed(coverage_contract, previous_manifest)
    encoded = {name: canonical_json_bytes(payload) for name, payload in payloads.items()}
    artifact_names = list(REQUIRED_ARTIFACT_NAMES)
    if set(encoded) != set(artifact_names):
        raise ReadModelError("read_model_artifact_set_incomplete")
    for name, content in encoded.items():
        if len(content) > MAX_READ_MODEL_BYTES:
            raise ReadModelError(f"read_model_too_large:{name}")
    observed_at = _observed_at_utc(effective_snapshot)
    source_dates = _source_dates(effective_snapshot)
    capability_states = None
    if capability_contract_version is not None:
        if capability_contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ReadModelError("capability_contract_version_unsupported")
        capability_states = build_capability_states(
            scope="full",
            as_of=str(snapshot["asOf"]),
            source_dates=source_dates,
            snapshot=effective_snapshot,
            payloads=payloads,
            stock_catalog=stock_catalog,
            artifact_names=set(encoded),
        )
    release_contract = {
        "asOf": snapshot["asOf"],
        "observedAt": observed_at,
        "sourceDates": source_dates,
        "marketHistory": market_history_contract,
        "coverage": coverage_contract,
        "pressureSchemaVersion": PRESSURE_SCHEMA_VERSION,
        "stockShardCount": STOCK_SHARD_COUNT,
        "requiredArtifacts": artifact_names,
    }
    if capability_states is not None:
        release_contract["capabilityContract"] = capability_contract_identity(capability_states, "full")
    if producer_build_sha:
        release_contract["producerBuildSha"] = producer_build_sha
    # Include release semantics in the identity, not only the payload bytes.
    # A corrected source timestamp or source-date mapping must therefore get a
    # new immutable release even when the visible JSON happens to be equal.
    digest_material = canonical_json_bytes(release_contract) + b"\n" + b"".join(
        encoded[name] for name in artifact_names
    )
    release_hash = sha256_bytes(digest_material)
    release_id = f"release:{snapshot['asOf']}:{release_hash[:16]}"
    expected_prefix = f"market-data/releases/{snapshot['asOf']}/{release_id}"
    if capability_states is not None and object_prefix is not None and object_prefix != expected_prefix:
        raise ReadModelError("capability_manifest_requires_release_scoped_artifacts")
    prefix = object_prefix or expected_prefix
    artifacts = {
        name: {
            "key": f"{prefix}/{name}.json",
            "sha256": sha256_bytes(encoded[name]),
            "bytes": len(encoded[name]),
            "contentType": "application/json; charset=utf-8",
        }
        for name in artifact_names
    }
    manifest = {
        "schemaVersion": CAPABILITY_MANIFEST_SCHEMA_VERSION if capability_states is not None else SCHEMA_VERSION,
        "releaseId": release_id,
        "asOf": snapshot["asOf"],
        "observedAt": observed_at,
        "sourceDates": source_dates,
        "marketHistory": market_history_contract,
        "coverage": coverage_contract,
        # These are fixed contract declarations.  A release without the
        # stock sidecars is still useful to unit-test the view splitter, but
        # the importer/history picker will reject it as incomplete.
        "pressureSchemaVersion": PRESSURE_SCHEMA_VERSION,
        "stockShardCount": STOCK_SHARD_COUNT,
        "requiredArtifacts": artifact_names,
        "classificationVersion": (snapshot.get("etfTerminal") or {}).get("classificationVersion"),
        "calculationVersion": (snapshot.get("etfTerminal") or {}).get("calculation"),
        "artifacts": artifacts,
        "quality": snapshot.get("quality"),
        "publication": snapshot.get("publication"),
    }
    if capability_states is not None:
        manifest["capabilityContract"] = bind_capability_artifact_refs(capability_states, "full", artifacts)
    if producer_build_sha:
        manifest["producerBuildSha"] = producer_build_sha
    # Canonicalize the manifest itself to ensure callers cannot accidentally
    # publish a different hash for the same view payloads.
    manifest["manifestSha256"] = sha256_bytes(canonical_json_bytes(manifest))
    return payloads, manifest


def build_market_read_models(
    snapshot: dict[str, Any],
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    market_history: dict[str, Any] | list[dict[str, Any]] | None = None,
    previous_manifest: dict[str, Any] | None = None,
    producer_build_sha: str | None = None,
    capability_contract_version: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build the core market release without requiring ETF PCF.

    The dashboard slot contains only views whose source contract is complete
    once the exchange ETF, margin and connect snapshots are ready.  PCF and
    the ETF-to-stock graph are intentionally absent; they are published in a
    separate ``etf_penetration`` slot when their own date gate passes.
    """

    if "etfReplayInput" in snapshot:
        from .etf_replay import replay_snapshot
        if capability_contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ReadModelError("etf_replay_candidate_contract_required")
        snapshot = replay_snapshot(snapshot)
    if "windowEvidence" in (snapshot.get("etfTerminal") or {}) and capability_contract_version != CAPABILITY_CONTRACT_VERSION:
        raise ReadModelError("etf_replay_candidate_contract_required")
    validate_snapshot(snapshot, require_materialized=require_materialized)
    compact_history = None
    effective_snapshot = snapshot
    if market_history is not None:
        compact_history, margin_series, connect_series, coverage = _normalize_market_history(
            market_history,
            as_of=str(snapshot["asOf"]),
        )
        effective_snapshot = dict(snapshot)
        effective_snapshot["marginSeries"] = margin_series
        effective_snapshot["connectSeries"] = connect_series
        effective_snapshot["marketHistoryCoverage"] = coverage
    _assert_history_not_regressed(_market_history_contract(compact_history), previous_manifest)

    all_payloads = build_view_payloads(effective_snapshot)
    payloads = {name: all_payloads[name] for name in MARKET_READ_MODEL_NAMES}
    encoded = {name: canonical_json_bytes(payload) for name, payload in payloads.items()}
    for name, content in encoded.items():
        if len(content) > MAX_READ_MODEL_BYTES:
            raise ReadModelError(f"read_model_too_large:{name}")
        _assert_finite(payloads[name])

    terminal = effective_snapshot.get("etfTerminal")
    terminal = terminal if isinstance(terminal, dict) else {}
    history = terminal.get("historyCoverage")
    history = history if isinstance(history, dict) else {}
    national_data = payloads["national"].get("data")
    national_data = national_data if isinstance(national_data, dict) else {}
    national_terminal = national_data.get("etfTerminal")
    national_terminal = national_terminal if isinstance(national_terminal, dict) else {}
    national_items = national_terminal.get("instruments")
    national_items = national_items if isinstance(national_items, list) else []
    coverage_contract = {
        "etf": {
            "tradingDays": int(history.get("tradingDays") or 0),
            "instrumentCount": len(terminal.get("instruments") or []),
            "availablePeriods": sorted(str(period) for period in (terminal.get("availablePeriods") or [])),
        },
        "national": {"count": len(national_items)},
    }
    # The independent core slot still carries the ETF history contract. Do
    # not let a PCF-independent rebuild silently shrink the periods already
    # visible in the dashboard.
    _assert_read_model_coverage_not_regressed(coverage_contract, previous_manifest)
    market_history_contract = _market_history_contract(compact_history)
    observed_at = _observed_at_utc(effective_snapshot)
    source_dates = _source_dates(effective_snapshot)
    capability_states = None
    if capability_contract_version is not None:
        if capability_contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ReadModelError("capability_contract_version_unsupported")
        capability_states = build_capability_states(
            scope="dashboard",
            as_of=str(snapshot["asOf"]),
            source_dates=source_dates,
            snapshot=effective_snapshot,
            payloads=payloads,
            stock_catalog=None,
            artifact_names=set(encoded),
        )
    release_contract = {
        "slot": "dashboard",
        "asOf": snapshot["asOf"],
        "observedAt": observed_at,
        "sourceDates": source_dates,
        "marketHistory": market_history_contract,
        "coverage": coverage_contract,
        "requiredArtifacts": list(MARKET_READ_MODEL_NAMES),
        "artifactSha256": {name: sha256_bytes(encoded[name]) for name in MARKET_READ_MODEL_NAMES},
    }
    if capability_states is not None:
        release_contract["capabilityContract"] = capability_contract_identity(capability_states, "dashboard")
    if producer_build_sha:
        release_contract["producerBuildSha"] = producer_build_sha
    release_hash = sha256_bytes(canonical_json_bytes(release_contract))
    release_id = f"release:{snapshot['asOf']}:{release_hash[:16]}"
    expected_prefix = f"market-data/releases/{snapshot['asOf']}/{release_id}"
    if capability_states is not None and object_prefix is not None and object_prefix != expected_prefix:
        raise ReadModelError("capability_manifest_requires_release_scoped_artifacts")
    prefix = object_prefix or expected_prefix
    artifacts = {
        name: {
            "key": f"{prefix}/{name}.json",
            "sha256": sha256_bytes(encoded[name]),
            "bytes": len(encoded[name]),
            "contentType": "application/json; charset=utf-8",
        }
        for name in MARKET_READ_MODEL_NAMES
    }
    manifest: dict[str, Any] = {
        "schemaVersion": CAPABILITY_MANIFEST_SCHEMA_VERSION if capability_states is not None else SCHEMA_VERSION,
        "slot": "dashboard",
        "releaseId": release_id,
        "asOf": snapshot["asOf"],
        "observedAt": observed_at,
        "sourceDates": source_dates,
        "marketHistory": market_history_contract,
        "coverage": coverage_contract,
        # The v3 contract keeps these declarations on every slot so the
        # Worker can apply one bounded capability check to both slots.
        "pressureSchemaVersion": PRESSURE_SCHEMA_VERSION,
        "stockShardCount": STOCK_SHARD_COUNT,
        "requiredArtifacts": list(MARKET_READ_MODEL_NAMES),
        "classificationVersion": terminal.get("classificationVersion"),
        "calculationVersion": terminal.get("calculation"),
        "artifacts": artifacts,
        "quality": snapshot.get("quality"),
        "publication": snapshot.get("publication"),
    }
    if capability_states is not None:
        manifest["capabilityContract"] = bind_capability_artifact_refs(capability_states, "dashboard", artifacts)
    if producer_build_sha:
        manifest["producerBuildSha"] = producer_build_sha
    manifest["manifestSha256"] = sha256_bytes(canonical_json_bytes(manifest))
    return payloads, manifest


def write_read_models(
    snapshot: dict[str, Any],
    output_dir: Path,
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    stock_catalog_path: Path | None = None,
    market_history_path: Path | None = None,
    previous_manifest_path: Path | None = None,
    producer_build_sha: str | None = None,
    capability_contract_version: str | None = None,
) -> dict[str, Any]:
    stock_catalog = None
    if stock_catalog_path is not None:
        stock_catalog = json.loads(stock_catalog_path.read_text(encoding="utf-8"))
    market_history = None
    if market_history_path is not None:
        market_history = json.loads(market_history_path.read_text(encoding="utf-8"))
    previous_manifest = None
    if previous_manifest_path is not None and previous_manifest_path.is_file():
        previous_manifest = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
    payloads, manifest = build_read_models(
        snapshot,
        require_materialized=require_materialized,
        object_prefix=object_prefix,
        stock_catalog=stock_catalog,
        market_history=market_history,
        previous_manifest=previous_manifest,
        producer_build_sha=producer_build_sha,
        capability_contract_version=capability_contract_version,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (output_dir / f"{name}.json").write_bytes(canonical_json_bytes(payload))
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return {"manifest": manifest, "manifestPath": str(manifest_path)}


def write_market_read_models(
    snapshot: dict[str, Any],
    output_dir: Path,
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    market_history_path: Path | None = None,
    previous_manifest_path: Path | None = None,
    producer_build_sha: str | None = None,
    capability_contract_version: str | None = None,
) -> dict[str, Any]:
    market_history = None
    if market_history_path is not None:
        market_history = json.loads(market_history_path.read_text(encoding="utf-8"))
    previous_manifest = None
    if previous_manifest_path is not None and previous_manifest_path.is_file():
        previous_manifest = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
    payloads, manifest = build_market_read_models(
        snapshot,
        require_materialized=require_materialized,
        object_prefix=object_prefix,
        market_history=market_history,
        previous_manifest=previous_manifest,
        producer_build_sha=producer_build_sha,
        capability_contract_version=capability_contract_version,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (output_dir / f"{name}.json").write_bytes(canonical_json_bytes(payload))
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return {"manifest": manifest, "manifestPath": str(manifest_path)}
