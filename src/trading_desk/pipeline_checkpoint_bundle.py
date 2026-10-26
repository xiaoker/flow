"""Bounded immutable stage bundles; transport and lease authority stay external.

No remote calls or archive extraction: a future R2 transport stores these exact
bytes by digest. Verification never interprets an existing file as completion.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .pipeline_application import ApplicationIdentity, canonical_sha256, checkpoint_is_reusable

MAX_FILES = 256
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 24_000_000
MAX_BUNDLE_BYTES = 34_000_000
# Existing client/server wire cap remains unchanged. Large v1 JSON bundles
# use a versioned compressed envelope; expanded limits still apply separately.
MAX_WIRE_BYTES = 1024 * 1024


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
    return encode_stage_bundle(files=files, checkpoint=checkpoint)


def _gzip_bytes(data: bytes) -> bytes:
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
        stream.write(data)
    return compressed.getvalue()


def _bundle_wire(value: dict[str, Any]) -> bytes:
    value["bundleManifestSha256"] = canonical_sha256(value)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if len(data) > MAX_BUNDLE_BYTES:
        raise CheckpointBundleError("bundle_byte_limit")
    if len(data) > MAX_WIRE_BYTES:
        # GzipFile fixes the OS byte as well as time/name, unlike older
        # gzip.compress(mtime=0) implementations delegated to platform zlib.
        envelope = {"schemaVersion": 2, "encoding": "gzip-base64",
                    "decodedBytes": len(data), "decodedSha256": hashlib.sha256(data).hexdigest(),
                    "data": base64.b64encode(_gzip_bytes(data)).decode("ascii")}
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    return data


def encode_stage_bundle(*, files: list[dict[str, Any]], checkpoint: Mapping[str, Any]) -> tuple[bytes, str]:
    """Keep v1/v2 wire compatibility; v3 compresses exact files before base64.

    Nested archive base64 otherwise inflates compressed checkpoints. No evidence
    is omitted and wire/expanded limits are unchanged.
    """
    files = sorted(files, key=lambda item: item["path"])
    data = _bundle_wire({"schemaVersion": 1, "checkpoint": dict(checkpoint), "files": files})
    if len(data) > MAX_WIRE_BYTES:
        compressed_files = []
        total = 0
        for entry in files:
            content = base64.b64decode(entry["data"], validate=True)
            total += len(content)
            if len(content) != entry["bytes"] or len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise CheckpointBundleError("bundle_byte_limit")
            compressed_files.append({**entry, "encoding": "gzip-base64",
                "data": base64.b64encode(_gzip_bytes(content)).decode("ascii")})
        data = _bundle_wire({"schemaVersion": 3, "checkpoint": dict(checkpoint), "files": compressed_files})
    if len(data) > MAX_WIRE_BYTES:
        raise CheckpointBundleError("bundle_transport_size_exceeded")
    return data, hashlib.sha256(data).hexdigest()


def verify_stage_bundle(data: bytes, *, sha256: str, identity: ApplicationIdentity,
                        stage: str, input_sha256: str) -> tuple[Mapping[str, Any], dict[str, bytes]]:
    """Verify all bytes before any caller restores files or skips a stage."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BUNDLE_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise CheckpointBundleError("bundle_digest_or_size_mismatch")
    try:
        value = json.loads(data)
        if isinstance(value, dict) and value.get("schemaVersion") == 2:
            if (len(data) > MAX_WIRE_BYTES or set(value) != {"schemaVersion", "encoding", "decodedBytes", "decodedSha256", "data"}
                    or value["encoding"] != "gzip-base64" or type(value["decodedBytes"]) is not int
                    or not 0 < value["decodedBytes"] <= MAX_BUNDLE_BYTES):
                raise CheckpointBundleError("invalid_compressed_bundle_schema")
            compressed = base64.b64decode(value["data"], validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                expanded = stream.read(min(value["decodedBytes"], MAX_BUNDLE_BYTES) + 1)
            if len(expanded) != value["decodedBytes"] or hashlib.sha256(expanded).hexdigest() != value["decodedSha256"]:
                raise CheckpointBundleError("compressed_bundle_digest_or_size_mismatch")
            value = json.loads(expanded)
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "checkpoint", "files", "bundleManifestSha256"} or value["schemaVersion"] not in {1, 3}:
            raise CheckpointBundleError("invalid_bundle_schema")
        file_compressed = value["schemaVersion"] == 3
        if file_compressed and len(data) > MAX_WIRE_BYTES:
            raise CheckpointBundleError("bundle_transport_size_exceeded")
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
            fields = {"path", "bytes", "sha256", "data"} | ({"encoding"} if file_compressed else set())
            if not isinstance(entry, dict) or set(entry) != fields:
                raise CheckpointBundleError("invalid_bundle_file")
            name = _path(entry["path"])
            if type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_FILE_BYTES or total + entry["bytes"] > MAX_TOTAL_BYTES:
                raise CheckpointBundleError("bundle_file_size_or_duplicate")
            content = base64.b64decode(entry["data"], validate=True)
            if file_compressed:
                if entry["encoding"] != "gzip-base64":
                    raise CheckpointBundleError("invalid_bundle_file_encoding")
                with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                    content = stream.read(entry["bytes"] + 1)
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
    except (ValueError, TypeError, KeyError, OSError, EOFError, zlib.error) as exc:
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
