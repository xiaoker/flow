"""Derive a bounded archive recipe from an immutable collector history bundle.

The result is an *input candidate*, not a recovery approval.  The existing
archive reader still verifies every object against R2 before using it.
"""
from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import zlib
from typing import Any

from .phase0_archive import CONTRACT, MAX_OBJECT_BYTES, MAX_TOTAL_BYTES, validate_recipe
from .etf_period_dependencies import OFFICIAL_MARGIN_DATES_SOURCE, validate_official_calendar


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    # Offline candidates can reconstruct current observation metadata only.
    # Live recipes must bind the actual first-observation bytes retained in R2.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _content(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > ((MAX_OBJECT_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("archive_recipe_content_missing")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("archive_recipe_content_invalid") from exc


def _normalized_content(wire: bytes, encoding: str | None) -> bytes:
    if not isinstance(wire, bytes) or len(wire) > MAX_OBJECT_BYTES:
        raise ValueError("archive_recipe_normalized_size_invalid")
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
                decoded = stream.read(MAX_OBJECT_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError("archive_recipe_normalized_gzip_invalid") from exc
    elif encoding is None:
        decoded = wire
    else:
        raise ValueError("archive_recipe_normalized_encoding_invalid")
    if len(decoded) > MAX_OBJECT_BYTES:
        raise ValueError("archive_recipe_normalized_size_invalid")
    return decoded


def _same_observation_values(reference: bytes, stored: bytes) -> bool:
    """Only capture time may vary at legacy raw-SHA normalized keys."""
    try:
        rows = [list(csv.reader(io.StringIO(value.decode("utf-8")))) for value in (reference, stored)]
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("archive_recipe_stored_normalized_invalid") from exc
    if rows[0] == rows[1]:
        return True
    if not rows[0] or not rows[1] or rows[0][0] != rows[1][0]:
        return False
    header = rows[0][0]
    if not header or len(set(header)) != len(header) or "observed_at" not in header:
        return False
    if any(len(row) != len(header) for table in rows for row in table[1:]):
        return False
    index = header.index("observed_at")
    values = [[row[:index] + row[index + 1:] for row in table[1:]] for table in rows]
    return values[0] == values[1]


def official_calendar_from_bundle(*, manifest: dict[str, Any], history_bundle: dict[str, Any]) -> dict[str, Any]:
    """Prove the three requested business dates from the bundled official SSE response."""
    requested = manifest.get("requested_dates")
    history = history_bundle.get("history") if history_bundle.get("version") == 1 else None
    if (not isinstance(requested, list) or len(requested) != 3
            or not all(isinstance(day, str) for day in requested)
            or requested != sorted(set(requested)) or not isinstance(history, dict)
            or history.get("runId") != manifest.get("run_id")
            or history.get("asOf") != requested[-1]
            or not isinstance(history.get("artifacts"), list) or len(history["artifacts"]) > 24):
        raise ValueError("archive_recipe_calendar_identity_invalid")
    source_rows = [part for part in history["artifacts"] if isinstance(part, dict)
                   and part.get("sourceId") == "sse" and part.get("dataset") == "margin_market_daily"
                   and part.get("requestedDate") == f"{requested[0]}_{requested[-1]}"]
    items = manifest.get("artifacts")
    if not isinstance(items, list) or len(items) > 24:
        raise ValueError("archive_recipe_calendar_source_missing")
    manifest_rows = [item for item in items if isinstance(item, dict)
                     and isinstance(item.get("source"), dict)
                     and item["source"].get("source_id") == "sse"
                     and item["source"].get("dataset") == "margin_market_daily"
                     and item.get("requested_date") == f"{requested[0]}_{requested[-1]}"]
    if len(source_rows) != 1 or len(manifest_rows) != 1:
        raise ValueError("archive_recipe_calendar_source_missing")
    source, item = source_rows[0], manifest_rows[0]
    raw = _content(source.get("rawContentBase64"))
    if (len(raw) > MAX_OBJECT_BYTES or _sha(raw) != source.get("sha256")
            or source.get("sha256") != item.get("sha256")):
        raise ValueError("archive_recipe_calendar_source_hash_mismatch")
    try:
        response = json.loads(raw)
        rows = response["result"]
        dates = sorted(f"{str(row['opDate'])[:4]}-{str(row['opDate'])[4:6]}-{str(row['opDate'])[6:]}" for row in rows)
        begin, end = response["beginDate"], response["endDate"]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("archive_recipe_calendar_source_invalid") from exc
    if (dates != requested or begin != requested[0].replace("-", "")
            or end != requested[-1].replace("-", "")):
        raise ValueError("archive_recipe_calendar_dates_mismatch")
    calendar = {"sourceId": OFFICIAL_MARGIN_DATES_SOURCE, "sha256": _sha(raw),
                "dates": dates, "datesSha256": _sha(_json_bytes(dates)),
                "evidence": {"contentBase64": base64.b64encode(raw).decode(),
                             "rangeStart": requested[0], "rangeEnd": requested[-1]}}
    validate_official_calendar(calendar)
    return calendar


def adjacent_object_from_bundle(*, manifest: dict[str, Any], previous_bundle: dict[str, Any]) -> dict[str, bytes]:
    """Recover an exact adjacent ETF object from a prior immutable Collector bundle."""
    adjacent = manifest.get("archivedEtfInputs", [])
    if not isinstance(adjacent, list) or len(adjacent) != 1 or not isinstance(adjacent[0], dict):
        raise ValueError("archive_recipe_adjacent_invalid")
    ref = adjacent[0]
    history = previous_bundle.get("history") if previous_bundle.get("version") == 1 else None
    if (not isinstance(history, dict) or not isinstance(history.get("artifacts"), list)
            or len(history["artifacts"]) > 24 or history.get("asOf") != ref.get("tradeDate")):
        raise ValueError("archive_recipe_previous_history_invalid")
    rows = [part for part in history["artifacts"] if isinstance(part, dict)
            and part.get("sourceId") == "szse" and part.get("dataset") == "etf_scale"
            and part.get("requestedDate") == ref.get("tradeDate")]
    if len(rows) != 1 or ref.get("sourceId") != "szse":
        raise ValueError("archive_recipe_adjacent_source_missing")
    source = rows[0]
    raw = _content(source.get("rawContentBase64"))
    raw_sha = source.get("sha256")
    metadata = source.get("metadata")
    if (len(raw) > MAX_OBJECT_BYTES or _sha(raw) != raw_sha
            or not isinstance(metadata, dict) or metadata.get("sha256") != raw_sha):
        raise ValueError("archive_recipe_adjacent_source_hash_mismatch")
    parts = source.get("normalized")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict) or parts[0].get("name") != "data":
        raise ValueError("archive_recipe_adjacent_normalized_invalid")
    part = parts[0]
    wire = _content(part.get("contentBase64"))
    if len(wire) > MAX_OBJECT_BYTES:
        raise ValueError("archive_recipe_adjacent_normalized_invalid")
    if part.get("contentEncoding") == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
                decoded = stream.read(MAX_OBJECT_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise ValueError("archive_recipe_adjacent_normalized_invalid") from exc
        suffix = ".csv.gz"
    elif part.get("contentEncoding") is None:
        decoded = wire
        suffix = ".csv"
    else:
        raise ValueError("archive_recipe_adjacent_normalized_invalid")
    if len(decoded) > MAX_OBJECT_BYTES or _sha(decoded) != part.get("sha256"):
        raise ValueError("archive_recipe_adjacent_normalized_hash_mismatch")
    revision = _sha(f"{raw_sha}:{_sha(wire)}".encode())
    key = f"market-data/normalized/szse/etf_scale/{ref.get('tradeDate')}/{revision}/data{suffix}"
    if (ref.get("revisionId") != revision or ref.get("objectKey") != key
            or ref.get("sha256") != _sha(wire)):
        raise ValueError("archive_recipe_adjacent_identity_mismatch")
    return {key: wire}


def build_archive_recipe(
    *, manifest: dict[str, Any], history_bundle: dict[str, Any],
    calendar: dict[str, Any], target: str,
    adjacent_objects: dict[str, bytes] | None = None,
    metadata_objects: dict[str, bytes] | None = None,
    normalized_objects: dict[str, bytes] | None = None,
    legacy_etf_objects: dict[str, bytes] | None = None,
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
    used_metadata: set[str] = set()
    used_normalized: set[str] = set()
    used_legacy_etf: set[str] = set()

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
        metadata_key = f"{prefix}.metadata.json"
        metadata_bytes = _json_bytes({**metadata, "release_id": artifact.get("releaseId"), "archived_raw_key": raw_key})
        if metadata_objects is not None:
            metadata_bytes = metadata_objects.get(metadata_key)
            if not isinstance(metadata_bytes, bytes) or len(metadata_bytes) > MAX_OBJECT_BYTES:
                raise ValueError("archive_recipe_stored_metadata_missing")
            try:
                stored = json.loads(metadata_bytes)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("archive_recipe_stored_metadata_invalid") from exc
            # The historical per-fund composite predates generic transport
            # metadata. Its exact original bytes/hash are already verified;
            # accept an absent length only for this precise legacy contract,
            # without rewriting the retained metadata or inventing HTTP 200.
            legacy_length_unrecorded = (
                isinstance(stored, dict) and "bytes" not in stored
                and source_id == "szse" and dataset == "etf_scale"
                and source.get("date_semantics") == "source_historical_scale_size_date"
                and source.get("url") in {
                    "https://fund.szse.cn/api/report/ShowReport/data",
                    "https://www.szse.cn/api/report/ShowReport/data",
                }
                and stored.get("source") == source
            )
            if (not isinstance(stored, dict) or stored.get("sha256") != raw_sha
                    or stored.get("requested_date") != day or stored.get("release_id") != artifact.get("releaseId")
                    or stored.get("archived_raw_key") != raw_key or stored.get("status_code") != item.get("status_code")
                    or (not legacy_length_unrecorded and (
                        type(stored.get("bytes")) is not int or stored["bytes"] != len(raw)))
                    or not isinstance(stored.get("observed_at"), str)
                    or not stored["observed_at"] or not isinstance(stored.get("source"), dict)
                    or stored["source"].get("source_id") != source_id or stored["source"].get("dataset") != dataset):
                raise ValueError("archive_recipe_stored_metadata_invalid")
            used_metadata.add(metadata_key)
        add(item["metadata_path"], metadata_key, metadata_bytes)
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
            decoded = _normalized_content(wire, encoding)
            if len(decoded) > MAX_OBJECT_BYTES or _sha(decoded) != part.get("sha256"):
                raise ValueError("archive_recipe_normalized_hash_mismatch")
            revision = _sha(f"{raw_sha}:{_sha(wire)}".encode()) if dataset == "etf_scale" else raw_sha
            suffix = ".csv.gz" if encoding == "gzip" else ".csv"
            key = f"market-data/normalized/{source_id}/{dataset}/{day}/{revision}/{name}{suffix}"
            legacy_key = f"market-data/normalized/{source_id}/{dataset}/{day}/{raw_sha}/{name}.csv"
            if dataset == "etf_scale" and legacy_key in (legacy_etf_objects or {}):
                # Explicit, hash-read legacy bytes only. Never silently fall
                # back after a failed new-key read or fabricate a new object.
                actual = legacy_etf_objects[legacy_key]
                if encoding is not None or not _same_observation_values(decoded, _normalized_content(actual, None)):
                    raise ValueError("archive_recipe_legacy_etf_values_mismatch")
                key, wire = legacy_key, actual
                used_legacy_etf.add(key)
            if normalized_objects is not None and dataset != "etf_scale":
                actual = normalized_objects.get(key)
                if actual is None:
                    raise ValueError("archive_recipe_stored_normalized_missing")
                if not _same_observation_values(decoded, _normalized_content(actual, encoding)):
                    raise ValueError("archive_recipe_stored_normalized_values_mismatch")
                wire = actual
                used_normalized.add(key)
            add(path, key, wire)

    if metadata_objects is not None and set(metadata_objects) != used_metadata:
        raise ValueError("archive_recipe_stored_metadata_unexpected")
    if normalized_objects is not None and set(normalized_objects) != used_normalized:
        raise ValueError("archive_recipe_stored_normalized_unexpected")
    if legacy_etf_objects is not None and set(legacy_etf_objects) != used_legacy_etf:
        raise ValueError("archive_recipe_legacy_etf_unexpected")
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
