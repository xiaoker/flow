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


SCHEMA_VERSION = 2
VIEW_NAMES = ("bootstrap", "overview", "radar", "rotation", "national", "connect", "margin")
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
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return view payloads and a release manifest without touching storage."""

    validate_snapshot(snapshot, require_materialized=require_materialized)
    payloads = build_view_payloads(snapshot)
    encoded = {name: canonical_json_bytes(payload) for name, payload in payloads.items()}
    digest_material = b"".join(encoded[name] for name in VIEW_NAMES)
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
        for name in VIEW_NAMES
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
) -> dict[str, Any]:
    payloads, manifest = build_read_models(
        snapshot,
        require_materialized=require_materialized,
        object_prefix=object_prefix,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (output_dir / f"{name}.json").write_bytes(canonical_json_bytes(payload))
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return {"manifest": manifest, "manifestPath": str(manifest_path)}
