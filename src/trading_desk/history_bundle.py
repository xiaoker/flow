from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import FetchArtifact


# Zero is the wire-contract sentinel for indefinite R2 retention. Raw objects
# are additionally protected by an R2 bucket lock on market-data/raw/.
HISTORY_RETENTION_DAYS = 0


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def _relative_or_absolute(path: str, workspace_root: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else workspace_root / candidate


def _normalized_files(
    artifact: dict[str, Any], workspace_root: Path
) -> list[dict[str, Any]]:
    value = str(artifact.get("normalized_path") or "")
    if not value:
        raise ValueError("Artifact did not contain a normalized path")
    paths = [_relative_or_absolute(item, workspace_root) for item in value.split(";")]
    dataset = str((artifact.get("source") or {}).get("dataset") or "")
    names = (
        ["summary", "top"]
        if dataset == "stock_connect_daily" and len(paths) == 2
        else [f"part-{index + 1}" for index in range(len(paths))]
    )
    if len(paths) == 1:
        names = ["data"]

    output: list[dict[str, Any]] = []
    for name, path in zip(names, paths, strict=True):
        if not path.is_file():
            raise ValueError(f"Normalized file does not exist: {path}")
        content = path.read_bytes()
        try:
            frame = pd.read_csv(path)
            row_count = len(frame)
            trade_dates = sorted(
                frame.get("trade_date", pd.Series(dtype="string"))
                .dropna()
                .astype(str)
                .unique()
                .tolist()
            )
        except pd.errors.EmptyDataError:
            row_count = 0
            trade_dates = []
        encoded_content = content
        content_encoding: str | None = None
        # PCF is the only intentionally wide normalized feed.  Compressing
        # its CSV in transit keeps each resumable history request well below
        # the Worker 12 MB body cap while the archived normalized R2 object
        # remains the ordinary, human-readable CSV after server-side decode.
        if dataset == "etf_pcf" and content:
            encoded_content = gzip.compress(content, compresslevel=6)
            content_encoding = "gzip"
        item = {
            "name": name,
            "format": "csv",
            "sha256": hashlib.sha256(content).hexdigest(),
            "rowCount": row_count,
            "tradeDates": trade_dates,
            "contentBase64": base64.b64encode(encoded_content).decode("ascii"),
        }
        if content_encoding:
            item["contentEncoding"] = content_encoding
        output.append(item)
    return output


def serialize_history_artifact(
    artifact: FetchArtifact | dict[str, Any], *, workspace_root: Path
) -> dict[str, Any]:
    payload = artifact.to_dict() if isinstance(artifact, FetchArtifact) else artifact
    source = payload.get("source") or {}
    source_id = str(source.get("source_id") or "")
    dataset = str(source.get("dataset") or "")
    requested_date = str(payload.get("requested_date") or "current")
    if source_id not in {"sse", "szse", "bse", "hkex"}:
        raise ValueError(f"Unsupported history source: {source_id!r}")
    if dataset not in {
        "etf_scale",
        "etf_nav",
        "etf_pcf",
        "margin_market_daily",
        "margin_security_daily",
        "stock_connect_daily",
    }:
        raise ValueError(f"Unsupported history dataset: {dataset!r}")
    if not re.fullmatch(r"[0-9_-]{8,25}|current", requested_date):
        raise ValueError(f"Invalid history partition: {requested_date!r}")

    raw_path = _relative_or_absolute(str(payload.get("raw_path") or ""), workspace_root)
    metadata_path = _relative_or_absolute(
        str(payload.get("metadata_path") or ""), workspace_root
    )
    if not raw_path.is_file() or not metadata_path.is_file():
        raise ValueError(f"Raw history artifact files are incomplete for {source_id}/{dataset}")
    raw_content = raw_path.read_bytes()
    digest = hashlib.sha256(raw_content).hexdigest()
    if digest != str(payload.get("sha256") or ""):
        raise ValueError(f"Raw SHA-256 mismatch for {raw_path}")

    normalized = _normalized_files(payload, workspace_root)
    trade_dates = sorted(
        {
            trade_date
            for item in normalized
            for trade_date in item.get("tradeDates", [])
        }
    )
    extension = raw_path.suffix.lower().lstrip(".")
    return {
        "releaseId": f"{dataset}:{source_id}:{requested_date}:{digest[:16]}",
        "sourceId": source_id,
        "dataset": dataset,
        "requestedDate": requested_date,
        "tradeDates": trade_dates,
        "observedAt": str(payload.get("observed_at") or ""),
        "sourceUrl": str(source.get("url") or ""),
        "dateBasis": str(source.get("date_semantics") or "source_published"),
        "sha256": digest,
        "contentType": str(payload.get("content_type") or "application/octet-stream"),
        "extension": extension,
        "rawContentBase64": base64.b64encode(raw_content).decode("ascii"),
        "metadata": _read_json(metadata_path),
        "normalized": normalized,
    }


def build_history_bundle(
    *,
    snapshot: dict[str, Any],
    artifacts: Iterable[FetchArtifact | dict[str, Any]],
    run_id: str,
    started_at: str,
    finished_at: str,
    workspace_root: Path,
) -> dict[str, Any]:
    serialized = [
        serialize_history_artifact(artifact, workspace_root=workspace_root)
        for artifact in artifacts
    ]
    if not serialized:
        raise ValueError("History bundle did not contain any source artifacts")
    return {
        "version": 1,
        "history": {
            "runId": run_id,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "retentionDays": HISTORY_RETENTION_DAYS,
            "asOf": str(snapshot.get("asOf") or ""),
            "snapshot": snapshot,
            "artifacts": serialized,
        },
    }


def build_history_bundle_from_files(
    *, snapshot_path: Path, manifest_path: Path, workspace_root: Path
) -> dict[str, Any]:
    snapshot = _read_json(snapshot_path)
    manifest = _read_json(manifest_path)
    return build_history_bundle(
        snapshot=snapshot,
        artifacts=manifest.get("artifacts") or [],
        run_id=str(manifest.get("run_id") or ""),
        started_at=str(manifest.get("started_at") or ""),
        finished_at=str(manifest.get("finished_at") or ""),
        workspace_root=workspace_root,
    )
