"""Derive a bounded archive recipe from an immutable collector history bundle.

The result is an *input candidate*, not a recovery approval.  The existing
archive reader still verifies every object against R2 before using it.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
from typing import Any

from .phase0_archive import CONTRACT, MAX_OBJECT_BYTES, MAX_TOTAL_BYTES, validate_recipe


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    # Match JSON.stringify's ordinary object layout used by history-import.
    # A remote byte read remains authoritative if an exotic metadata value is
    # serialized differently by JavaScript.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _content(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > ((MAX_OBJECT_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("archive_recipe_content_missing")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("archive_recipe_content_invalid") from exc


def build_archive_recipe(
    *, manifest: dict[str, Any], history_bundle: dict[str, Any],
    calendar: dict[str, Any], target: str,
    adjacent_objects: dict[str, bytes] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return the exact-key recipe and locally reconstructed stored bytes.

    Caller must supply the separately proven official calendar.  No missing
    artifact is inferred, no bucket is listed, and no remote write occurs.
    """
    history = history_bundle.get("history") if history_bundle.get("version") == 1 else None
    if not isinstance(history, dict) or not isinstance(history.get("artifacts"), list):
        raise ValueError("archive_recipe_history_invalid")
    if history.get("runId") != manifest.get("run_id") or history.get("asOf") != target:
        raise ValueError("archive_recipe_history_identity_mismatch")
    items = manifest.get("artifacts")
    if not isinstance(items, list) or len(items) != len(history["artifacts"]):
        raise ValueError("archive_recipe_artifact_count_mismatch")

    refs: list[dict[str, Any]] = []
    objects: dict[str, bytes] = {}

    def add(path: str, key: str, content: bytes) -> None:
        if key in objects or len(content) > MAX_OBJECT_BYTES:
            raise ValueError("archive_recipe_object_invalid")
        objects[key] = content
        refs.append({"path": path, "objectKey": key, "sha256": _sha(content), "bytes": len(content)})

    for item, artifact in zip(items, history["artifacts"], strict=True):
        if not isinstance(item, dict) or not isinstance(artifact, dict):
            raise ValueError("archive_recipe_artifact_invalid")
        source = item.get("source") or {}
        source_id, dataset, day, raw_sha = (
            source.get("source_id"), source.get("dataset"), item.get("requested_date"), item.get("sha256"))
        if (artifact.get("sourceId"), artifact.get("dataset"), artifact.get("requestedDate"), artifact.get("sha256")) != (
            source_id, dataset, day, raw_sha):
            raise ValueError("archive_recipe_artifact_identity_mismatch")
        raw = _content(artifact.get("rawContentBase64"))
        if _sha(raw) != raw_sha:
            raise ValueError("archive_recipe_raw_hash_mismatch")
        extension = artifact.get("extension")
        if not isinstance(extension, str) or not item.get("raw_path", "").endswith("." + extension):
            raise ValueError("archive_recipe_raw_extension_mismatch")
        prefix = f"market-data/raw/{source_id}/{dataset}/{day}/{raw_sha}"
        raw_key = f"{prefix}.{extension}"
        add(item["raw_path"], raw_key, raw)
        metadata = artifact.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("sha256") != raw_sha:
            raise ValueError("archive_recipe_metadata_mismatch")
        add(item["metadata_path"], f"{prefix}.metadata.json", _json_bytes({
            **metadata, "release_id": artifact.get("releaseId"), "archived_raw_key": raw_key}))
        parts = item.get("normalized_path", "").split(";")
        normalized = artifact.get("normalized")
        names = ["summary", "top"] if len(parts) == 2 else ["data"]
        if not isinstance(normalized, list) or len(parts) != len(normalized):
            raise ValueError("archive_recipe_normalized_count_mismatch")
        for path, name, part in zip(parts, names, normalized, strict=True):
            if not isinstance(part, dict) or part.get("name") != name:
                raise ValueError("archive_recipe_normalized_identity_mismatch")
            wire = _content(part.get("contentBase64"))
            encoding = part.get("contentEncoding")
            if encoding == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
                        decoded = stream.read(MAX_OBJECT_BYTES + 1)
                except (OSError, EOFError) as exc:
                    raise ValueError("archive_recipe_normalized_gzip_invalid") from exc
            elif encoding is None:
                decoded = wire
            else:
                raise ValueError("archive_recipe_normalized_encoding_invalid")
            if len(decoded) > MAX_OBJECT_BYTES or _sha(decoded) != part.get("sha256"):
                raise ValueError("archive_recipe_normalized_hash_mismatch")
            revision = _sha(f"{raw_sha}:{_sha(wire)}".encode()) if dataset == "etf_scale" else raw_sha
            suffix = ".csv.gz" if encoding == "gzip" else ".csv"
            key = f"market-data/normalized/{source_id}/{dataset}/{day}/{revision}/{name}{suffix}"
            add(path, key, wire)

    adjacent = manifest.get("archivedEtfInputs", [])
    if not isinstance(adjacent, list):
        raise ValueError("archive_recipe_adjacent_invalid")
    # An adjacent archive reference is not in this history bundle.  It must be
    # supplied as a separately verified exact object, never guessed.
    for adjacent_ref in adjacent:
        if not isinstance(adjacent_ref, dict):
            raise ValueError("archive_recipe_adjacent_invalid")
        key = adjacent_ref.get("objectKey")
        content = (adjacent_objects or {}).get(key)
        if content is None:
            raise ValueError("archive_recipe_adjacent_object_required")
        if _sha(content) != adjacent_ref.get("sha256"):
            raise ValueError("archive_recipe_adjacent_hash_mismatch")
        add(adjacent_ref["normalizedPath"], key, content)
    if sum(ref["bytes"] for ref in refs) > MAX_TOTAL_BYTES:
        raise ValueError("archive_recipe_total_bytes_exceeded")
    encoded = _json_bytes(manifest)
    recipe = {"contract": CONTRACT, "manifestGzipBase64": base64.b64encode(gzip.compress(encoded, mtime=0)).decode(),
              "manifestSha256": _sha(encoded), "calendar": calendar, "files": refs}
    validate_recipe(recipe, target)
    return recipe, objects
