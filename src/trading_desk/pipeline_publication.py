"""Shared read-model publication contract; no HTTP or storage operations.

The existing publisher remains the caller. Candidate runner integration must
separately supply authenticated, bounded transport and resource admission.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from typing import Any

from .read_models import canonical_json_bytes

ACTIVATION_MODES = {"active", "stage_only"}


def build_candidate_upload_body(manifest_bytes: bytes, name: str, content: bytes,
                                *, execution: dict[str, Any], finalize: bool) -> dict[str, Any]:
    """Keep the producer's exact signed canonical bytes across JS serialization.

    Pure preparation only: no permission, budget, HTTP or success assertion.
    The server still verifies current authority and immutable byte identity.
    """
    if not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= 65536:
        raise ValueError("candidate_manifest_bytes_limit")
    if not isinstance(content, bytes) or not 0 < len(content) <= 1_500_000 or type(finalize) is not bool:
        raise ValueError("candidate_artifact_bytes_limit")
    try:
        manifest = json.loads(manifest_bytes)
    except (ValueError, UnicodeError):
        raise ValueError("candidate_manifest_invalid") from None
    if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != manifest_bytes:
        raise ValueError("candidate_manifest_not_canonical")
    unsigned = dict(manifest)
    declared_hash = unsigned.pop("manifestSha256", None)
    if declared_hash != hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest():
        raise ValueError("candidate_manifest_digest_mismatch")
    if manifest.get("schemaVersion") != 4 or not isinstance(execution, dict):
        raise ValueError("candidate_upload_contract_invalid")
    if execution.get("targetDate") != manifest.get("asOf") or execution.get("producerBuildSha") != manifest.get("producerBuildSha"):
        raise ValueError("candidate_upload_identity_mismatch")
    artifacts = manifest.get("artifacts")
    declared = artifacts.get(name) if isinstance(artifacts, dict) else None
    if not isinstance(declared, dict) or declared.get("sha256") != hashlib.sha256(content).hexdigest() or declared.get("bytes") != len(content):
        raise ValueError("candidate_artifact_identity_mismatch")
    body = build_upload_body(manifest, name, declared, content, finalize=finalize, activation_mode="stage_only")
    body["pipelineExecution"] = dict(execution)
    body["candidateManifestJson"] = manifest_bytes.decode("utf-8")
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > 1_500_000:
        raise ValueError("candidate_upload_request_limit")
    return body


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
