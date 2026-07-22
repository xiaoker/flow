from __future__ import annotations

import base64
import hashlib
import json
import math
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


def _etf_artifact(artifact: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
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
        rows.append(
            {
                "code": str(row["instrument_code"]).strip().zfill(6),
                "name": str(row["instrument_name"]).strip(),
                "category": category,
                "shares": _integer(row["shares"], field="shares"),
                "navMicros": _nav_micros(row.get("nav")),
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
    *, snapshot_path: Path, manifest_path: Path, workspace_root: Path
) -> dict[str, Any]:
    snapshot = _read_json(snapshot_path)
    manifest = _read_json(manifest_path)
    artifacts = [
        _etf_artifact(artifact, workspace_root)
        for artifact in manifest.get("artifacts") or []
        if (artifact.get("source") or {}).get("dataset") == "etf_scale"
    ]
    if not artifacts:
        raise ValueError("Manifest did not contain any ETF artifacts")
    return {
        "version": 1,
        "snapshot": snapshot,
        "archive": {
            "runId": str(manifest.get("run_id", "")),
            "startedAt": str(manifest.get("started_at", "")),
            "finishedAt": str(manifest.get("finished_at", "")),
            "retentionDays": RETENTION_DAYS,
            "artifacts": artifacts,
        },
    }
