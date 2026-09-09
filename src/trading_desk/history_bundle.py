from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .etf_daily_changes import build_official_etf_daily_changes
from .etf_facts import build_canonical_etf_daily_fact
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


def _artifact_payload(artifact: FetchArtifact | dict[str, Any]) -> dict[str, Any]:
    return artifact.to_dict() if isinstance(artifact, FetchArtifact) else artifact


def _load_etf_scale_frame(
    artifact: FetchArtifact | dict[str, Any], *, workspace_root: Path
) -> pd.DataFrame:
    """Read the normalized ETF scale parts used by the canonical writer.

    The collector may keep several semicolon-separated parts for a source.
    Reading those parts here, before they are encoded for transport, makes the
    adjacent-change contract byte-independent and keeps its input identical to
    the dashboard's canonical ETF frame.
    """

    payload = _artifact_payload(artifact)
    normalized_path = str(payload.get("normalized_path") or "")
    if not normalized_path:
        raise ValueError("etf_scale_artifact_missing_normalized_path")
    frames: list[pd.DataFrame] = []
    for path_value in normalized_path.split(";"):
        path = _relative_or_absolute(path_value, workspace_root)
        if not path.is_file():
            raise ValueError(f"Normalized ETF file does not exist: {path}")
        try:
            frames.append(
                pd.read_csv(
                    path,
                    dtype={"instrument_code": str},
                )
            )
        except pd.errors.EmptyDataError:
            frames.append(pd.DataFrame())
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def _build_official_etf_daily_changes_from_artifacts(
    *,
    artifacts: list[FetchArtifact | dict[str, Any]],
    serialized: list[dict[str, Any]],
    as_of: str,
    workspace_root: Path,
) -> dict[str, Any] | None:
    scale_artifacts = [
        artifact
        for artifact in artifacts
        if str((_artifact_payload(artifact).get("source") or {}).get("dataset") or "")
        == "etf_scale"
    ]
    if not scale_artifacts:
        return None

    by_market: dict[str, list[pd.DataFrame]] = {"sse": [], "szse": []}
    release_ids: list[str] = []
    for artifact in scale_artifacts:
        payload = _artifact_payload(artifact)
        source_id = str((payload.get("source") or {}).get("source_id") or "")
        if source_id not in by_market:
            raise ValueError(f"Unsupported ETF scale source for adjacent changes: {source_id!r}")
        frame = _load_etf_scale_frame(artifact, workspace_root=workspace_root)
        # A collection window may contain a newer source response than the
        # snapshot selected for publication (for example, a 09-08 ETF file
        # alongside a 09-07 fallback snapshot).  That newer response remains
        # immutable archive evidence, but it cannot participate in an
        # as-of-bounded adjacent-change contract.  Passing it to the strict
        # canonical builder would make a valid fallback fail with
        # ``canonical_etf_future_date`` before the archive can be persisted.
        eligible_dates = (
            frame.get("trade_date", pd.Series(dtype="string"))
            .dropna()
            .astype(str)
            .loc[lambda values: values <= as_of]
            .unique()
            .tolist()
        )
        if not frame.empty and eligible_dates:
            by_market[source_id].append(frame)
        # Use the same release id that is sent over the wire, rather than a
        # second hash implementation.  This lets the server verify source
        # lineage without reading the normalized CSV again.
        matching = [
            item
            for item in serialized
            if item.get("dataset") == "etf_scale"
            and item.get("sourceId") == source_id
            and item.get("requestedDate") == str(payload.get("requested_date") or "")
            and item.get("sha256") == str(payload.get("sha256") or "")
        ]
        if matching and eligible_dates:
            release_ids.append(str(matching[0]["releaseId"]))
        elif str(payload.get("sha256") or ""):
            # This branch is defensive for a malformed caller; normal bundle
            # construction always has a matching serialized artifact.
            raise ValueError("etf_scale_serialized_release_missing")

    sse = pd.concat(by_market["sse"], ignore_index=True, sort=False) if by_market["sse"] else pd.DataFrame()
    szse = pd.concat(by_market["szse"], ignore_index=True, sort=False) if by_market["szse"] else pd.DataFrame()
    if sse.empty and szse.empty:
        return None
    dates = sorted(
        {
            str(value)
            for frame in (sse, szse)
            if not frame.empty and "trade_date" in frame.columns
            for value in frame["trade_date"].dropna().astype(str).unique().tolist()
            if value <= as_of
        }
    )
    # A pending/fallback snapshot can have no ETF rows at its selected as-of
    # date after future responses are excluded.  In that case there is no
    # adjacent contract to publish; archive the source evidence without
    # inventing an empty or stale change set.
    if as_of not in dates:
        return None
    canonical = build_canonical_etf_daily_fact(sse, szse, dates)
    return build_official_etf_daily_changes(
        canonical,
        as_of=as_of,
        source_release_ids=release_ids,
    )


def build_history_bundle(
    *,
    snapshot: dict[str, Any],
    artifacts: Iterable[FetchArtifact | dict[str, Any]],
    run_id: str,
    started_at: str,
    finished_at: str,
    workspace_root: Path,
) -> dict[str, Any]:
    artifact_list = list(artifacts)
    serialized = [
        serialize_history_artifact(artifact, workspace_root=workspace_root)
        for artifact in artifact_list
    ]
    if not serialized:
        raise ValueError("History bundle did not contain any source artifacts")
    history: dict[str, Any] = {
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "retentionDays": HISTORY_RETENTION_DAYS,
        "asOf": str(snapshot.get("asOf") or ""),
        "snapshot": snapshot,
        "artifacts": serialized,
    }
    as_of = str(snapshot.get("asOf") or "")
    if as_of:
        adjacent = _build_official_etf_daily_changes_from_artifacts(
            artifacts=artifact_list,
            serialized=serialized,
            as_of=as_of,
            workspace_root=workspace_root,
        )
        if adjacent is not None:
            history["officialEtfDailyChanges"] = adjacent
    return {
        "version": 1,
        "history": history,
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
