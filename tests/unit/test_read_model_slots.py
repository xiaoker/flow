from __future__ import annotations

import json
from pathlib import Path

from scripts.split_read_model_slots import SLOT_ARTIFACTS, split


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    artifacts = {}
    for name in {name for names in SLOT_ARTIFACTS.values() for name in names}:
        content = json.dumps({"name": name}, separators=(",", ":")).encode()
        (source / f"{name}.json").write_bytes(content)
        import hashlib

        artifacts[name] = {
            "key": f"market-data/releases/2026-08-21/source/{name}.json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 3,
                "releaseId": "release:2026-08-21:0000000000000000",
                "asOf": "2026-08-21",
                "observedAt": "2026-08-21T12:00:00.000Z",
                "sourceDates": {"snapshot": "2026-08-21"},
                "pressureSchemaVersion": 2,
                "stockShardCount": 8,
                "requiredArtifacts": list(artifacts),
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return source


def test_split_creates_independent_slot_manifests(tmp_path: Path) -> None:
    source = _source(tmp_path)
    market = split(source, tmp_path / "market", "dashboard")
    penetration = split(source, tmp_path / "penetration", "etf_penetration")

    assert market["slot"] == "dashboard"
    assert market["requiredArtifacts"] == list(SLOT_ARTIFACTS["dashboard"])
    assert set(market["artifacts"]) == set(SLOT_ARTIFACTS["dashboard"])
    assert penetration["slot"] == "etf_penetration"
    assert penetration["requiredArtifacts"] == list(SLOT_ARTIFACTS["etf_penetration"])
    assert set(penetration["artifacts"]) == set(SLOT_ARTIFACTS["etf_penetration"])
    assert market["releaseId"] != penetration["releaseId"]
    assert (tmp_path / "market" / "pressure.json").exists() is False
    assert (tmp_path / "penetration" / "pressure.json").exists()
