"""Shared read-model publication contract; no HTTP or storage operations.

The existing publisher remains the caller. Candidate runner integration must
separately supply authenticated, bounded transport and resource admission.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
from typing import Any

ACTIVATION_MODES = {"active", "stage_only"}


def validate_publication_capabilities(payload: Any, *, slot: str = "full",
                                      required_manifest_schema: int = 3) -> None:
    if slot not in {"dashboard", "etf_penetration", "full"}:
        raise ValueError(f"unsupported release slot: {slot}")
    if not isinstance(payload, dict):
        raise RuntimeError("capabilities response is not an object")
    required = {
        "readModelManifestSchemas": required_manifest_schema,
        "schemaVersions": required_manifest_schema,
        "pressureSchemaVersions": 2,
        "stockShardCounts": 8,
    }
    for field, version in required.items():
        values = payload.get(field)
        if not isinstance(values, list) or version not in values:
            raise RuntimeError(f"deployed capability missing {field}={version}")
    features = set(payload.get("features") or [])
    required_features = {"requiredArtifacts", "sameDateObservedAtOrder", "releaseSlots",
                         "bseMarginSeparateScope"}
    if slot in {"etf_penetration", "full"}:
        required_features.update({"previousCompatibleRelease", "radarFlowView", "sourceReadinessLedger"})
    if required_manifest_schema == 4:
        required_features.add("capabilityContractV07")
    if not required_features.issubset(features):
        raise RuntimeError(f"deployed capability lacks v{required_manifest_schema} release features")


def build_upload_body(manifest: dict[str, Any], name: str, declared: dict[str, Any],
                      content: bytes, *, finalize: bool,
                      activation_mode: str = "active") -> dict[str, Any]:
    """Preserve the existing upload contract, including explicit stage_only.

    This constructs a request only. It does not prove the declared manifest or
    grant permission to upload. Schema-v4 manifests remain candidate-only.
    """
    if activation_mode not in ACTIVATION_MODES:
        raise ValueError(f"unsupported activation mode: {activation_mode}")
    if manifest.get("schemaVersion") == 4 and activation_mode != "stage_only":
        raise ValueError("schema-v4 capability manifests are candidate-only and require --stage-only")
    digest = hashlib.sha256(content).hexdigest()
    wire_content = gzip.compress(content, compresslevel=6)
    return {
        "version": 1,
        "activationMode": activation_mode,
        "manifest": manifest,
        "artifact": {
            "name": name, "key": declared["key"], "sha256": digest,
            "bytes": len(content),
            "contentBase64": base64.b64encode(wire_content).decode("ascii"),
            "contentEncoding": "gzip",
        },
        "finalize": finalize,
    }
