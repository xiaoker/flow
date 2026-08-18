from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd


RETENTION_DAYS = 365


def _clean_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any, *, field: str) -> int:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return int(round(number))


def _nav_micros(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(round(number * 1_000_000))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _stable_ingest_run_id(
    snapshot: dict[str, Any],
    artifacts: list[dict[str, Any]],
    read_manifest: dict[str, Any] | None = None,
    read_artifacts: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Build an idempotency key from immutable evidence, not wall-clock time.

    Cloudflare Cron and the GitHub failover schedule can collect the same
    source release.  The key stays identical for byte-identical evidence, but
    changes whenever an official source or read-model artifact is revised.
    """
    basis = {
        "asOf": str(snapshot.get("asOf", "")),
        "artifacts": sorted(
            [
                {
                    "releaseId": str(artifact.get("releaseId", "")),
                    "sha256": str(artifact.get("sha256", "")),
                }
                for artifact in artifacts
            ],
            key=lambda item: (item["releaseId"], item["sha256"]),
        ),
        "readModelReleaseId": str((read_manifest or {}).get("releaseId", "")),
        "readModels": sorted(
            [
                {"name": str(name), "sha256": str(metadata.get("sha256", ""))}
                for name, metadata in (read_artifacts or {}).items()
            ],
            key=lambda item: item["name"],
        ),
    }
    digest = hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return f"bundle:{basis['asOf']}:{digest}"


def _etf_artifact(
    artifact: dict[str, Any],
    workspace_root: Path,
    nav_lookup: dict[tuple[str, str, str], tuple[float, str | None]],
) -> dict[str, Any]:
    source = artifact.get("source") or {}
    source_id = str(source.get("source_id", ""))
    if source_id not in {"sse", "szse"}:
        raise ValueError(f"Unsupported ETF source: {source_id!r}")

    raw_path = workspace_root / str(artifact.get("raw_path", ""))
    metadata_path = workspace_root / str(artifact.get("metadata_path", ""))
    normalized_value = str(artifact.get("normalized_path", ""))
    if not raw_path.is_file() or not metadata_path.is_file() or not normalized_value:
        raise ValueError(f"ETF artifact files are incomplete for {source_id}")
    normalized_path = workspace_root / normalized_value
    if not normalized_path.is_file():
        raise ValueError(f"Normalized ETF file does not exist: {normalized_path}")

    content = raw_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != str(artifact.get("sha256", "")):
        raise ValueError(f"Raw SHA-256 mismatch for {raw_path}")

    metadata = _read_json(metadata_path)
    frame = pd.read_csv(normalized_path, dtype={"instrument_code": "string"})
    if frame.empty:
        raise ValueError(f"Normalized ETF file is empty: {normalized_path}")
    trade_dates = sorted(frame["trade_date"].dropna().astype(str).unique().tolist())
    requested_date = str(artifact.get("requested_date") or "")
    if trade_dates != [requested_date]:
        raise ValueError(
            f"ETF normalized date does not match artifact date: {trade_dates!r} != {requested_date!r}"
        )

    rows: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        category = _clean_text(row.get("investment_type")) or _clean_text(
            row.get("etf_type")
        )
        code = str(row["instrument_code"]).strip().zfill(6)
        trade_date = str(row["trade_date"])
        nav = row.get("nav")
        nav_date = _clean_text(row.get("nav_date"))
        if nav is None or pd.isna(nav):
            nav_value = nav_lookup.get((trade_date, "SSE" if source_id == "sse" else "SZSE", code))
            if nav_value is not None:
                nav, nav_date = nav_value
        rows.append(
            {
                "code": code,
                "name": str(row["instrument_name"]).strip(),
                "category": category,
                "shares": _integer(row["shares"], field="shares"),
                "navMicros": _nav_micros(nav),
                "navDate": nav_date,
                "dateBasis": str(row["date_basis"]),
            }
        )

    extension = raw_path.suffix.lower().lstrip(".")
    return {
        "releaseId": f"etf:{source_id}:{requested_date}:{digest[:16]}",
        "sourceId": source_id,
        "market": "SSE" if source_id == "sse" else "SZSE",
        "tradeDate": requested_date,
        "observedAt": str(artifact.get("observed_at", "")),
        "sourceUrl": str(source.get("url", "")),
        "dateBasis": str(source.get("date_semantics", "")),
        "sha256": digest,
        "contentType": str(artifact.get("content_type", "application/octet-stream")),
        "extension": extension,
        "rawContentBase64": base64.b64encode(content).decode("ascii"),
        "metadata": metadata,
        "rows": rows,
    }


def build_ingest_bundle(
    *,
    snapshot_path: Path,
    manifest_path: Path,
    workspace_root: Path,
    read_model_dir: Path | None = None,
) -> dict[str, Any]:
    snapshot = _read_json(snapshot_path)
    manifest = _read_json(manifest_path)
    nav_lookup: dict[tuple[str, str, str], tuple[float, str | None]] = {}
    for nav_artifact in manifest.get("artifacts") or []:
        source = nav_artifact.get("source") or {}
        if source.get("dataset") != "etf_nav":
            continue
        normalized_value = str(nav_artifact.get("normalized_path") or "")
        if not normalized_value:
            continue
        nav_path = workspace_root / normalized_value.split(";", 1)[0]
        if not nav_path.is_file():
            continue
        try:
            nav_frame = pd.read_csv(nav_path, dtype={"instrument_code": "string"})
        except pd.errors.EmptyDataError:
            continue
        for row in nav_frame.to_dict(orient="records"):
            code = str(row.get("instrument_code") or "").strip().zfill(6)
            value = row.get("nav")
            if not re.fullmatch(r"\d{6}", code) or value is None or pd.isna(value):
                continue
            nav_lookup[(str(row.get("trade_date")), str(row.get("market")), code)] = (
                float(value),
                _clean_text(row.get("nav_date")),
            )

    artifacts = [
        _etf_artifact(artifact, workspace_root, nav_lookup)
        for artifact in manifest.get("artifacts") or []
        if (artifact.get("source") or {}).get("dataset") == "etf_scale"
    ]
    if not artifacts:
        raise ValueError("Manifest did not contain any ETF artifacts")
    result: dict[str, Any] = {
        "version": 1,
        "snapshot": snapshot,
        "archive": {
            "runId": "",
            "startedAt": str(manifest.get("started_at", "")),
            "finishedAt": str(manifest.get("finished_at", "")),
            "retentionDays": RETENTION_DAYS,
            "artifacts": artifacts,
        },
    }
    if read_model_dir is not None:
        read_manifest_path = read_model_dir / "manifest.json"
        if not read_manifest_path.is_file():
            raise ValueError(f"Read-model manifest does not exist: {read_manifest_path}")
        read_manifest = _read_json(read_manifest_path)
        read_artifacts: dict[str, Any] = {}
        for name, metadata in (read_manifest.get("artifacts") or {}).items():
            if not re.fullmatch(
                r"(bootstrap|overview|radar|radar_flow|rotation|national|connect|margin|pressure|stock_search|stock_[0-7])",
                str(name),
            ):
                raise ValueError(f"Invalid read-model name: {name!r}")
            path = read_model_dir / f"{name}.json"
            if not path.is_file():
                raise ValueError(f"Read-model artifact does not exist: {path}")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != str(metadata.get("sha256")):
                raise ValueError(f"Read-model SHA-256 mismatch for {path}")
            # Read models contain the full ETF history and the ETF→stock
            # contribution graph.  They are immutable JSON at rest, but
            # compressing the wire representation keeps the single signed
            # import request below the free Worker body limit.  `bytes` and
            # `sha256` deliberately continue to describe the uncompressed
            # object that is written to R2.
            wire_content = gzip.compress(content, compresslevel=6)
            read_artifacts[name] = {
                "key": str(metadata["key"]),
                "sha256": digest,
                "bytes": len(content),
                "contentBase64": base64.b64encode(wire_content).decode("ascii"),
                "contentEncoding": "gzip",
            }
        result["readModels"] = {"manifest": read_manifest, "artifacts": read_artifacts}
    result["archive"]["runId"] = _stable_ingest_run_id(
        snapshot,
        artifacts,
        (result.get("readModels") or {}).get("manifest"),
        (result.get("readModels") or {}).get("artifacts"),
    )
    return result
