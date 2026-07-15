from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .models import FetchArtifact, SourceSpec


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S%z")


def write_raw_response(
    *,
    root: Path,
    source: SourceSpec,
    requested_date: str | None,
    run_id: str,
    content: bytes,
    extension: str,
    status_code: int,
    content_type: str,
    request_params: dict[str, Any] | None = None,
) -> FetchArtifact:
    partition = requested_date or "current"
    directory = root / "raw" / source.source_id / source.dataset / partition
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / f"{run_id}.{extension.lstrip('.')}"
    raw_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    observed_at = now_iso()
    metadata_path = directory / f"{run_id}.metadata.json"
    metadata = {
        "source": asdict(source),
        "requested_date": requested_date,
        "observed_at": observed_at,
        "status_code": status_code,
        "content_type": content_type,
        "sha256": digest,
        "bytes": len(content),
        "request_params": request_params or {},
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return FetchArtifact(
        source=source,
        requested_date=requested_date,
        observed_at=observed_at,
        status_code=status_code,
        content_type=content_type,
        raw_path=str(raw_path),
        metadata_path=str(metadata_path),
        sha256=digest,
    )


def write_normalized_csv(
    *, root: Path, dataset: str, partition: str, run_id: str, frame: pd.DataFrame
) -> Path:
    directory = root / "normalized" / dataset / partition
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"{run_id}.csv"
    frame.to_csv(output_path, index=False, encoding="utf-8")
    return output_path


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

