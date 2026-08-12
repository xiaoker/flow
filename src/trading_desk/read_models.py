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
from pathlib import Path
from typing import Any

from .stock_pressure import build_stock_pressure, stock_shard_name


SCHEMA_VERSION = 2
VIEW_NAMES = ("bootstrap", "overview", "radar", "rotation", "national", "connect", "margin", "pressure")
STOCK_SEARCH_NAME = "stock_search"
STOCK_SHARD_COUNT = 8
STOCK_SHARD_NAMES = tuple(f"stock_{index}" for index in range(STOCK_SHARD_COUNT))
MAX_READ_MODEL_BYTES = 5_000_000
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def build_view_payloads(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split one validated snapshot into bounded, view-specific payloads."""

    terminal = snapshot["etfTerminal"]
    # The penetration model is only needed by ETF Radar detail drawers.  Keep
    # it out of the overview payload so the default page does not download a
    # potentially wide stock-contribution graph.
    overview = {
        key: value
        for key, value in snapshot.items()
        if key not in {"etfTerminal", "etfStockFlow"}
    }
    radar = {
        "etfTerminal": terminal,
        "etfStockFlow": snapshot.get("etfStockFlow"),
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
    connect = {
        "connect": snapshot.get("connect", []),
        "connectSeries": snapshot.get("connectSeries", []),
        "marketHistoryCoverage": snapshot.get("marketHistoryCoverage"),
        "dataStatus": [row for row in snapshot.get("dataStatus", []) if "互联" in str(row.get("name", ""))],
    }
    margin = {
        "marginSeries": snapshot.get("marginSeries", []),
        "marketHistoryCoverage": snapshot.get("marketHistoryCoverage"),
        "dataStatus": [row for row in snapshot.get("dataStatus", []) if "融资" in str(row.get("name", ""))],
    }
    national = {
        "etfTerminal": terminal,
        "asOf": snapshot["asOf"],
        "classificationVersion": terminal.get("classificationVersion"),
    }
    return {
        "bootstrap": snapshot,
        "overview": _with_release_meta(snapshot, overview),
        "radar": _with_release_meta(snapshot, radar),
        "rotation": _with_release_meta(snapshot, rotation),
        "national": _with_release_meta(snapshot, national),
        "connect": _with_release_meta(snapshot, connect),
        "margin": _with_release_meta(snapshot, margin),
    }


def build_read_models(
    snapshot: dict[str, Any],
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    stock_catalog: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return view payloads and a release manifest without touching storage."""

    validate_snapshot(snapshot, require_materialized=require_materialized)
    stock_flow = snapshot.get("etfStockFlow")
    if isinstance(stock_flow, dict) and stock_flow.get("available") is True and stock_catalog is None:
        # The radar payload contains only a bounded compatibility graph.  Once
        # the penetration model is available, publishing without its complete
        # search/shard sidecars creates a release that can show clickable
        # stocks but cannot resolve them on the detail page.
        raise ReadModelError("stock_catalog_required_for_available_flow")
    payloads = build_view_payloads(snapshot)
    if stock_catalog is not None:
        payloads.update(_build_stock_artifacts(stock_catalog, as_of=snapshot["asOf"]))
        # Keep the pressure page as a compact projection.  Full contributor
        # lists stay in the existing stock shards and are fetched only after a
        # user selects one stock.
        pressure = build_stock_pressure(
            stock_catalog,
            market_financing_balance_yuan=_market_financing_balance(snapshot),
            margin_as_of=snapshot["asOf"],
        )
        payloads["pressure"] = _with_release_meta(snapshot, {"pressure": pressure})
    encoded = {name: canonical_json_bytes(payload) for name, payload in payloads.items()}
    artifact_names = [name for name in VIEW_NAMES if name in encoded] + [
        name for name in (STOCK_SEARCH_NAME, *STOCK_SHARD_NAMES) if name in encoded
    ]
    for name, content in encoded.items():
        if len(content) > MAX_READ_MODEL_BYTES:
            raise ReadModelError(f"read_model_too_large:{name}")
    digest_material = b"".join(encoded[name] for name in artifact_names)
    release_hash = sha256_bytes(digest_material)
    release_id = f"release:{snapshot['asOf']}:{release_hash[:16]}"
    prefix = object_prefix or f"market-data/releases/{snapshot['asOf']}/{release_id}"
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
        "schemaVersion": SCHEMA_VERSION,
        "releaseId": release_id,
        "asOf": snapshot["asOf"],
        "observedAt": snapshot.get("observedAt"),
        "classificationVersion": (snapshot.get("etfTerminal") or {}).get("classificationVersion"),
        "calculationVersion": (snapshot.get("etfTerminal") or {}).get("calculation"),
        "artifacts": artifacts,
        "quality": snapshot.get("quality"),
        "publication": snapshot.get("publication"),
    }
    # Canonicalize the manifest itself to ensure callers cannot accidentally
    # publish a different hash for the same view payloads.
    manifest["manifestSha256"] = sha256_bytes(canonical_json_bytes(manifest))
    return payloads, manifest


def write_read_models(
    snapshot: dict[str, Any],
    output_dir: Path,
    *,
    require_materialized: bool = False,
    object_prefix: str | None = None,
    stock_catalog_path: Path | None = None,
) -> dict[str, Any]:
    stock_catalog = None
    if stock_catalog_path is not None:
        stock_catalog = json.loads(stock_catalog_path.read_text(encoding="utf-8"))
    payloads, manifest = build_read_models(
        snapshot,
        require_materialized=require_materialized,
        object_prefix=object_prefix,
        stock_catalog=stock_catalog,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (output_dir / f"{name}.json").write_bytes(canonical_json_bytes(payload))
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return {"manifest": manifest, "manifestPath": str(manifest_path)}
