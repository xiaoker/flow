from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from trading_desk.adapters.official import OfficialMarketAdapters, SSE_ETF, SZSE_ETF
from trading_desk.models import FetchArtifact
from trading_desk.release_qa import validate_artifact_lineage


def _artifact(
    *, source, raw_path: Path, metadata_path: Path, normalized_path: Path, digest: str
) -> dict:
    return {
        "source": {
            "source_id": source.source_id,
            "publisher": source.publisher,
            "dataset": source.dataset,
            "url": source.url,
            "source_grade": source.source_grade,
            "date_semantics": source.date_semantics,
        },
        "requested_date": "2026-07-15" if source.source_id == "sse" else None,
        "observed_at": "2026-07-16T00:10:00+08:00",
        "status_code": 200,
        "content_type": "application/json",
        "raw_path": str(raw_path),
        "metadata_path": str(metadata_path),
        "normalized_path": str(normalized_path),
        "sha256": digest,
        "row_count": 1,
    }


def test_release_qa_detects_cross_source_normalized_path_collision(tmp_path: Path) -> None:
    shared = tmp_path / "shared.csv"
    pd.DataFrame([{"market": "SZSE"}]).to_csv(shared, index=False)
    artifacts = []
    for source in (SSE_ETF, SZSE_ETF):
        raw = tmp_path / f"{source.source_id}.raw"
        raw.write_bytes(source.source_id.encode())
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        metadata = tmp_path / f"{source.source_id}.metadata.json"
        payload = {
            "source": {"source_id": source.source_id, "dataset": source.dataset},
            "requested_date": "2026-07-15" if source.source_id == "sse" else None,
            "status_code": 200,
            "sha256": digest,
        }
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        artifacts.append(
            _artifact(
                source=source,
                raw_path=raw,
                metadata_path=metadata,
                normalized_path=shared,
                digest=digest,
            )
        )

    findings = validate_artifact_lineage({"artifacts": artifacts}, Path("/"))
    collision = next(
        finding for finding in findings if finding.check_id == "lineage.normalized_paths_unique"
    )

    assert collision.status == "fail"
    assert str(shared) in collision.evidence["collisions"]


def test_release_qa_detects_tampered_raw_response(tmp_path: Path) -> None:
    raw = tmp_path / "source.json"
    raw.write_text("changed", encoding="utf-8")
    normalized = tmp_path / "normalized.csv"
    pd.DataFrame([{"market": "SSE"}]).to_csv(normalized, index=False)
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "source": {"source_id": "sse", "dataset": "etf_scale"},
                "requested_date": "2026-07-15",
                "status_code": 200,
                "sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "artifacts": [
            _artifact(
                source=SSE_ETF,
                raw_path=raw,
                metadata_path=metadata,
                normalized_path=normalized,
                digest="0" * 64,
            )
        ]
    }

    findings = validate_artifact_lineage(manifest, Path("/"))
    digest = next(finding for finding in findings if finding.check_id == "lineage.sha256_matches")

    assert digest.status == "fail"


def test_normalized_storage_is_namespaced_by_source(tmp_path: Path) -> None:
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path, run_id="run")  # type: ignore[arg-type]
    frame = pd.DataFrame([{"instrument_code": "510300"}])

    def persist(source) -> str:
        artifact = FetchArtifact(
            source=source,
            requested_date="2026-07-15",
            observed_at="2026-07-16T00:10:00+08:00",
            status_code=200,
            content_type="application/json",
            raw_path="raw",
            metadata_path="metadata",
            sha256="0" * 64,
        )
        _, saved = adapters._persist(frame, artifact, "2026-07-15")
        return str(saved.normalized_path)

    sse_path = persist(SSE_ETF)
    szse_path = persist(SZSE_ETF)

    assert sse_path != szse_path
    assert "sse_etf_scale" in sse_path
    assert "szse_etf_scale" in szse_path
