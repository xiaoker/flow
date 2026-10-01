"""Split one validated full read-model build into immutable release slots.

The producer can build the complete set once offline, then publish the core
market views and the PCF-dependent ETF penetration views independently.  This
script only rewrites the manifest/object prefix; it never changes payload
bytes or invents a missing penetration artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from trading_desk.read_model_capabilities import (
    CAPABILITY_MANIFEST_SCHEMA_VERSION,
    bind_capability_artifact_refs,
    capability_contract_identity,
    states_from_capability_contract,
    validate_capability_contract_for_split,
)


SLOT_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "dashboard": (
        "bootstrap",
        "overview",
        "radar",
        "rotation",
        "national",
        "connect",
        "margin",
    ),
    "etf_penetration": (
        "radar_flow",
        "pressure",
        "stock_search",
        "stock_0",
        "stock_1",
        "stock_2",
        "stock_3",
        "stock_4",
        "stock_5",
        "stock_6",
        "stock_7",
    ),
}
MAX_SOURCE_ARTIFACT_BYTES = 5_000_000


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def split(source_dir: Path, output_dir: Path, slot: str) -> dict[str, Any]:
    if slot not in SLOT_ARTIFACTS:
        raise ValueError(f"unsupported read-model slot: {slot}")
    source_manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(source_manifest, dict) or source_manifest.get("schemaVersion") not in {3, CAPABILITY_MANIFEST_SCHEMA_VERSION}:
        raise ValueError("source manifest must be schemaVersion 3 or 4")
    schema_version = int(source_manifest["schemaVersion"])
    as_of = source_manifest.get("asOf")
    if not isinstance(as_of, str) or len(as_of) != 10:
        raise ValueError("source manifest has no valid asOf")
    source_artifacts = source_manifest.get("artifacts")
    if not isinstance(source_artifacts, dict):
        raise ValueError("source manifest has no artifacts")

    names = SLOT_ARTIFACTS[slot]
    payloads: dict[str, bytes] = {}
    if schema_version == CAPABILITY_MANIFEST_SCHEMA_VERSION:
        source_contract = source_manifest.get("capabilityContract")
        validate_capability_contract_for_split(source_contract, source_manifest)
        full_names = tuple(dict.fromkeys(name for slot_names in SLOT_ARTIFACTS.values() for name in slot_names))
        required = source_manifest.get("requiredArtifacts")
        if (
            set(source_artifacts) != set(full_names)
            or not isinstance(required, list)
            or len(required) != len(full_names)
            or set(required) != set(full_names)
        ):
            raise ValueError("source v4 manifest artifact inventory is incomplete")
        # A slot projection must not hide a corrupt READY capability owned by
        # the other slot. The v4 source is a bounded, complete offline build.
        source_names = full_names
    else:
        source_names = names

    for name in source_names:
        if name not in source_artifacts:
            raise ValueError(f"source manifest is missing required artifact: {name}")
        path = source_dir / f"{name}.json"
        if not path.is_file():
            raise ValueError(f"source artifact is missing: {path}")
        if path.stat().st_size > MAX_SOURCE_ARTIFACT_BYTES:
            raise ValueError(f"source artifact exceeds size limit: {name}")
        content = path.read_bytes()
        declared = source_artifacts[name]
        if (
            not isinstance(declared, dict)
            or not isinstance(declared.get("bytes"), int)
            or isinstance(declared.get("bytes"), bool)
            or declared.get("bytes") <= 0
            or declared.get("bytes") > MAX_SOURCE_ARTIFACT_BYTES
            or declared.get("bytes") != len(content)
            or declared.get("sha256") != sha256(content)
        ):
            raise ValueError(f"source artifact digest mismatch: {name}")
        payloads[name] = content
    if schema_version == CAPABILITY_MANIFEST_SCHEMA_VERSION:
        payloads = {name: payloads[name] for name in names}

    release_contract = {
        "slot": slot,
        "asOf": as_of,
        "observedAt": source_manifest.get("observedAt"),
        "sourceDates": source_manifest.get("sourceDates", {}),
        "requiredArtifacts": names,
        "artifactSha256": {name: sha256(payloads[name]) for name in names},
    }
    capability_states = None
    if schema_version == CAPABILITY_MANIFEST_SCHEMA_VERSION:
        source_contract = source_manifest.get("capabilityContract")
        capability_states = states_from_capability_contract(source_contract, slot)
        release_contract["capabilityContract"] = capability_contract_identity(capability_states, slot)
    release_id = f"release:{as_of}:{sha256(canonical_json(release_contract))[:16]}"
    prefix = f"market-data/releases/{as_of}/{release_id}"
    artifacts = {
        name: {
            "key": f"{prefix}/{name}.json",
            "sha256": sha256(payloads[name]),
            "bytes": len(payloads[name]),
            "contentType": "application/json; charset=utf-8",
        }
        for name in names
    }
    manifest: dict[str, Any] = {
        "schemaVersion": schema_version,
        "slot": slot,
        "releaseId": release_id,
        "asOf": as_of,
        "observedAt": source_manifest.get("observedAt"),
        "sourceDates": source_manifest.get("sourceDates", {}),
        "requiredArtifacts": list(names),
        "artifacts": artifacts,
    }
    if capability_states is not None:
        manifest["capabilityContract"] = bind_capability_artifact_refs(capability_states, slot, artifacts)
    # Keep lineage and QA metadata from the full build.  They are descriptive
    # only; the slot identity above is part of the new immutable release hash.
    for key in ("marketHistory", "coverage", "pressureSchemaVersion", "stockShardCount", "classificationVersion", "calculationVersion", "quality", "publication", "producerBuildSha"):
        if key in source_manifest:
            manifest[key] = source_manifest[key]
    manifest["manifestSha256"] = sha256(canonical_json(manifest))

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in payloads.items():
        (output_dir / f"{name}.json").write_bytes(content)
    (output_dir / "manifest.json").write_bytes(canonical_json(manifest))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a full read-model build into a release slot")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--slot", choices=sorted(SLOT_ARTIFACTS), required=True)
    args = parser.parse_args()
    print(json.dumps(split(args.source_dir, args.output_dir, args.slot), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
