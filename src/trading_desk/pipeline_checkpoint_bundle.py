"""Bounded immutable stage bundles; transport and lease authority stay external.

No remote calls or archive extraction: a future R2 transport stores these exact
bytes by digest. Verification never interprets an existing file as completion.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .pipeline_application import ApplicationIdentity, canonical_sha256, checkpoint_is_reusable

MAX_FILES = 256
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 24_000_000
MAX_BUNDLE_BYTES = 34_000_000


class CheckpointBundleError(ValueError):
    pass


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or "\\" in value:
        raise CheckpointBundleError("invalid_bundle_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or "" in value.split("/"):
        raise CheckpointBundleError("invalid_bundle_path")
    if value == "memory" or value.startswith("memory/") or value.startswith("reports/phase0/latest-"):
        raise CheckpointBundleError("protected_bundle_path")
    return value


def _checkpoint(value: Mapping[str, Any], identity: ApplicationIdentity, stage: str, input_sha256: str) -> None:
    if not checkpoint_is_reusable(value, identity=identity, stage=stage, input_sha256=input_sha256):
        raise CheckpointBundleError("bundle_checkpoint_not_reusable")
    if not value.get("checkpointSha256") or not identity.producer_build_sha:
        raise CheckpointBundleError("bundle_identity_proof_missing")


def build_stage_bundle(*, workspace: Path, checkpoint: Mapping[str, Any], identity: ApplicationIdentity,
                       stage: str, input_sha256: str) -> tuple[bytes, str]:
    """Include every regular stage output, never links or partial file lists."""
    _checkpoint(checkpoint, identity, stage, input_sha256)
    if workspace.is_symlink() or not workspace.is_dir():
        raise CheckpointBundleError("invalid_bundle_workspace")
    files: list[dict[str, Any]] = []
    total = 0
    # Iteration is bounded before sorting; do not materialize an unlimited tree.
    pending = [workspace]
    visited = 0
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            visited += 1
            if visited > MAX_FILES * 2:
                raise CheckpointBundleError("bundle_entry_limit")
            if path.is_symlink():
                raise CheckpointBundleError("bundle_symlink_forbidden")
            name = _path(path.relative_to(workspace).as_posix())
            if path.is_dir():
                pending.append(path)
                continue
            if not path.is_file() or len(files) >= MAX_FILES:
                raise CheckpointBundleError("bundle_file_limit_or_type")
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise CheckpointBundleError("bundle_byte_limit")
            files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "data": base64.b64encode(data).decode("ascii")})
    if not files:
        raise CheckpointBundleError("bundle_outputs_missing")
    value = {"schemaVersion": 1, "checkpoint": dict(checkpoint), "files": sorted(files, key=lambda item: item["path"])}
    value["bundleManifestSha256"] = canonical_sha256(value)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(data) > MAX_BUNDLE_BYTES:
        raise CheckpointBundleError("bundle_byte_limit")
    return data, hashlib.sha256(data).hexdigest()


def verify_stage_bundle(data: bytes, *, sha256: str, identity: ApplicationIdentity,
                        stage: str, input_sha256: str) -> tuple[Mapping[str, Any], dict[str, bytes]]:
    """Verify all bytes before any caller restores files or skips a stage."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BUNDLE_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise CheckpointBundleError("bundle_digest_or_size_mismatch")
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "checkpoint", "files", "bundleManifestSha256"} or value["schemaVersion"] != 1:
            raise CheckpointBundleError("invalid_bundle_schema")
        if value["bundleManifestSha256"] != canonical_sha256({k: v for k, v in value.items() if k != "bundleManifestSha256"}):
            raise CheckpointBundleError("bundle_manifest_mismatch")
        checkpoint = value["checkpoint"]
        if not isinstance(checkpoint, dict):
            raise CheckpointBundleError("invalid_bundle_checkpoint")
        _checkpoint(checkpoint, identity, stage, input_sha256)
        entries = value["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise CheckpointBundleError("bundle_file_limit")
        result: dict[str, bytes] = {}
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256", "data"}:
                raise CheckpointBundleError("invalid_bundle_file")
            name = _path(entry["path"])
            content = base64.b64decode(entry["data"], validate=True)
            total += len(content)
            if name in result or isinstance(entry["bytes"], bool) or entry["bytes"] != len(content) or len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise CheckpointBundleError("bundle_file_size_or_duplicate")
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise CheckpointBundleError("bundle_file_digest_mismatch")
            result[name] = content
        if list(result) != sorted(result) or any(any(other.startswith(name + "/") for other in result) for name in result):
            raise CheckpointBundleError("bundle_path_conflict_or_order")
        return checkpoint, result
    except CheckpointBundleError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise CheckpointBundleError("invalid_bundle_encoding") from exc


def restore_stage_bundle(data: bytes, *, sha256: str, identity: ApplicationIdentity,
                         stage: str, input_sha256: str, workspace: Path,
                         confirm_lease: Callable[[], Any],
                         verify_output: Callable[..., bool],
                         check_lease: Callable[[], Any] | None = None) -> Mapping[str, Any]:
    """Restore only into a new isolated directory; partial restores fail closed.

    Completion is committed by the runner only AFTER this function succeeds.
    Interrupted directories remain for diagnosis; they are never overwritten or
    silently treated as completed. This is not a distributed lock or R2 client.
    """
    checkpoint, files = verify_stage_bundle(data, sha256=sha256, identity=identity,
                                           stage=stage, input_sha256=input_sha256)
    workspace = workspace.absolute()
    if any(path.is_symlink() for path in (workspace, *workspace.parents)):
        raise CheckpointBundleError("restore_symlink_forbidden")
    if "memory" in workspace.parts or any(
        workspace.parts[index:index + 2] == ("reports", "phase0")
        for index in range(len(workspace.parts) - 1)
    ):
        raise CheckpointBundleError("protected_restore_path")
    confirm_lease()
    workspace.parent.mkdir(parents=True, exist_ok=True)
    try:
        workspace.mkdir()  # Exclusive; never merge into an existing workspace.
    except FileExistsError as exc:
        raise CheckpointBundleError("restore_workspace_exists") from exc
    for name, content in files.items():
        (check_lease or confirm_lease)()
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    if verify_output(checkpoint=checkpoint, identity=identity, input_sha256=input_sha256,
                     workspace=workspace) is not True:
        raise CheckpointBundleError("restored_adapter_evidence_mismatch")
    confirm_lease()
    return checkpoint
