"""Read-only, bounded construction of an exact Phase 0 archive input candidate.

Live verification is opt-in.  It uses only the existing authenticated archive
GET endpoint; this command never registers a plan or writes production state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trading_desk.phase0_archive import MAX_FILES, MAX_OBJECT_BYTES, ArchivedOfficialMarketAdapters
from trading_desk.phase0_archive_recipe import (
    adjacent_object_from_bundle, build_archive_recipe, official_calendar_from_bundle,
)

ARCHIVE_ORIGIN = "https://flow.xiaoker.com"
MAX_BUNDLE_BYTES = 40_000_000
ARCHIVE_USER_AGENT = "FlowArchiveVerifier/1.0 (+https://flow.xiaoker.com)"


class ArchiveReadError(ValueError):
    def __init__(self, code: str, *, status: int | None = None, ray: str | None = None):
        super().__init__(code)
        self.status = status
        self.ray = ray if ray and re.fullmatch(r"[a-zA-Z0-9-]{1,80}", ray) else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _read_json(path: Path) -> dict:
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("archive_recipe_input_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("archive_recipe_input_invalid")
    return value


def _archive_get(key: str, digest: str, *, metadata_raw: bool = False, normalized_raw: bool = False) -> bytes:
    if metadata_raw and normalized_raw:
        raise ValueError("archive_recipe_discovery_mode_invalid")
    required = ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("archive_recipe_auth_missing")
    field = ("archive_metadata_raw_sha256" if metadata_raw else
             "archive_normalized_raw_sha256" if normalized_raw else "archive_sha256")
    query = urlencode({"archive_key": key, field: digest})
    request = Request(f"{ARCHIVE_ORIGIN}/api/v1/admin/history-artifact?{query}", headers={
        "Authorization": f"Bearer {os.environ['FLOW_INGEST_API_KEY']}",
        "CF-Access-Client-Id": os.environ["FLOW_CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["FLOW_CF_ACCESS_CLIENT_SECRET"],
        "User-Agent": ARCHIVE_USER_AGENT,
        "Accept": "application/octet-stream",
    })
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            actual = response.headers.get("x-flow-archive-sha256", "")
            if (response.status != 200 or response.headers.get("x-flow-archive-key") != key
                    or not re.fullmatch(r"[a-f0-9]{64}", actual)
                    or (response.headers.get("x-flow-archive-raw-sha256") != digest
                        if metadata_raw or normalized_raw else actual != digest)):
                raise ValueError("archive_recipe_remote_identity_mismatch")
            content = response.read(MAX_OBJECT_BYTES + 1)
    except HTTPError as error:
        # Do not persist arbitrary edge HTML, request headers, cookies or auth.
        ray = error.headers.get("cf-ray") if error.headers else None
        error.close()
        raise ArchiveReadError("archive_recipe_remote_http_error", status=error.code, ray=ray) from None
    except (URLError, TimeoutError):
        raise ArchiveReadError("archive_recipe_remote_transport_error") from None
    if len(content) > MAX_OBJECT_BYTES or hashlib.sha256(content).hexdigest() != actual:
        raise ValueError("archive_recipe_remote_hash_mismatch")
    return content


def _archive_metadata_get(key: str, raw_digest: str) -> bytes:
    return _archive_get(key, raw_digest, metadata_raw=True)


def _archive_normalized_get(key: str, raw_digest: str) -> bytes:
    return _archive_get(key, raw_digest, normalized_raw=True)


def _legacy_etf_refs(path: Path, bundle: dict, *, verify: bool) -> list[dict]:
    if not verify or path.stat().st_size > 16_000:
        raise ValueError("archive_recipe_legacy_etf_verification_required")
    bindings = _read_json(path)
    rows = bindings.get("objects")
    if (set(bindings) != {"contract", "objects"} or bindings["contract"] != "flow-legacy-etf-bindings-v1"
            or not isinstance(rows, list) or not 1 <= len(rows) <= 24):
        raise ValueError("archive_recipe_legacy_etf_bindings_invalid")
    # The caller has already validated manifest, raw hashes, calendar and scope.
    allowed = {f"market-data/normalized/{a['sourceId']}/etf_scale/{a['requestedDate']}/{a['sha256']}/data.csv"
               for a in bundle["history"]["artifacts"] if a["dataset"] == "etf_scale"}
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"objectKey", "sha256"}
                or not isinstance(row["objectKey"], str) or row["objectKey"] not in allowed
                or row["objectKey"] in seen or not isinstance(row["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
            raise ValueError("archive_recipe_legacy_etf_bindings_invalid")
        seen.add(row["objectKey"])
    return rows


def _build(args: argparse.Namespace, progress: dict) -> int:
    def read_remote(key: str, digest: str, *, metadata_raw: bool = False, normalized_raw: bool = False) -> bytes:
        progress["archiveReadAttempts"] += 1
        progress["failedObjectKey"] = key
        content = (_archive_metadata_get(key, digest) if metadata_raw else
                   _archive_normalized_get(key, digest) if normalized_raw else _archive_get(key, digest))
        progress["archiveReadCount"] += 1
        progress["failedObjectKey"] = None
        return content

    manifest = _read_json(args.manifest)
    bundle = _read_json(args.history_bundle)
    legacy_bindings = getattr(args, "legacy_etf_bindings", None)
    legacy_etf_objects = {}
    if args.previous_history_bundle and args.adjacent_file:
        raise ValueError("archive_recipe_adjacent_inputs_ambiguous")
    calendar = (_read_json(args.official_calendar) if args.official_calendar
                else official_calendar_from_bundle(manifest=manifest, history_bundle=bundle))
    adjacent_objects = {}
    live_verified_keys = set()
    adjacent = manifest.get("archivedEtfInputs", [])
    if adjacent:
        if len(adjacent) != 1 or not isinstance(adjacent[0], dict):
            raise ValueError("archive_recipe_adjacent_invalid")
        ref = adjacent[0]
        if args.previous_history_bundle:
            adjacent_objects = adjacent_object_from_bundle(
                manifest=manifest, previous_bundle=_read_json(args.previous_history_bundle))
        elif args.verify_live_archive:
            adjacent_objects[ref["objectKey"]] = read_remote(ref["objectKey"], ref["sha256"])
            live_verified_keys.add(ref["objectKey"])
        elif args.adjacent_file is not None:
            adjacent_objects[ref["objectKey"]] = args.adjacent_file.read_bytes()
        else:
            raise ValueError("archive_recipe_adjacent_object_required")
    recipe, objects = build_archive_recipe(
        manifest=manifest, history_bundle=bundle,
        calendar=calendar, target=args.target_date,
        adjacent_objects=adjacent_objects, legacy_etf_objects=legacy_etf_objects)
    if legacy_bindings is not None:
        refs = _legacy_etf_refs(legacy_bindings, bundle, verify=args.verify_live_archive)
        for ref in refs:
            wire = read_remote(ref["objectKey"], ref["sha256"])
            if hashlib.sha256(wire).hexdigest() != ref["sha256"]:
                raise ValueError("archive_recipe_legacy_etf_hash_mismatch")
            legacy_etf_objects[ref["objectKey"]] = wire
        recipe, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle,
            calendar=calendar, target=args.target_date, adjacent_objects=adjacent_objects,
            legacy_etf_objects=legacy_etf_objects)
    if args.verify_live_archive:
        if len(recipe["files"]) > MAX_FILES:
            raise ValueError("archive_recipe_file_limit_exceeded")
        # An unchanged raw response may already have older first-observation
        # metadata in R2. Resolve it by its exact raw identity, validate it,
        # then bind the actual bytes/hash before this recipe can be approved.
        metadata_objects = {}
        for ref in recipe["files"]:
            if ref["objectKey"].endswith(".metadata.json"):
                raw_digest = ref["objectKey"].rsplit("/", 1)[-1].split(".", 1)[0]
                metadata_objects[ref["objectKey"]] = read_remote(ref["objectKey"], raw_digest, metadata_raw=True)
        normalized_objects = {}
        for ref in recipe["files"]:
            key = ref["objectKey"]
            if key.startswith("market-data/normalized/") and key.split("/")[3] != "etf_scale":
                normalized_objects[key] = read_remote(key, key.split("/")[-2], normalized_raw=True)
        recipe, objects = build_archive_recipe(
            manifest=manifest, history_bundle=bundle, calendar=calendar, target=args.target_date,
            adjacent_objects=adjacent_objects, metadata_objects=metadata_objects,
            normalized_objects=normalized_objects, legacy_etf_objects=legacy_etf_objects)
        for ref in recipe["files"]:
            # Local adjacent bundle/file content is not proof that R2 holds it.
            # Only reuse bytes already read through the authenticated endpoint.
            remote = (legacy_etf_objects[ref["objectKey"]] if ref["objectKey"] in legacy_etf_objects
                      else metadata_objects[ref["objectKey"]] if ref["objectKey"] in metadata_objects
                      else normalized_objects[ref["objectKey"]] if ref["objectKey"] in normalized_objects
                      else adjacent_objects[ref["objectKey"]] if ref["objectKey"] in live_verified_keys
                      else read_remote(ref["objectKey"], ref["sha256"]))
            if remote != objects[ref["objectKey"]]:
                raise ValueError("archive_recipe_remote_content_mismatch")
        # Reuse the same raw-to-normalized and lineage QA as actual recovery.
        # Local materialization uses already-read bytes, no extra remote I/O.
        with tempfile.TemporaryDirectory(prefix="flow-archive-verify-") as directory:
            ArchivedOfficialMarketAdapters(recipe, target=args.target_date, root=Path(directory) / "input",
                                           fetch=lambda ref: objects[ref["objectKey"]])
    args.output.write_text(json.dumps({
        "status": "ARCHIVE_OBJECTS_VERIFIED" if args.verify_live_archive else "UNVERIFIED_CANDIDATE",
        "targetDate": args.target_date,
        "archiveReadCount": len(recipe["files"]) if args.verify_live_archive else 0,
        "archiveReadAttempts": progress["archiveReadAttempts"],
        "lineageVerified": bool(args.verify_live_archive),
        "recipe": recipe,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--history-bundle", type=Path, required=True)
    parser.add_argument("--official-calendar", type=Path)
    parser.add_argument("--previous-history-bundle", type=Path)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--verify-live-archive", action="store_true")
    parser.add_argument("--adjacent-file", type=Path)
    parser.add_argument("--legacy-etf-bindings", type=Path,
                        help="Explicit versioned legacy ETF key/hash list; requires --verify-live-archive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    progress = {"archiveReadAttempts": 0, "archiveReadCount": 0, "failedObjectKey": None}
    try:
        return _build(args, progress)
    except (ValueError, OSError) as error:
        code = str(error)
        args.output.write_text(json.dumps({
            "status": "ARCHIVE_VERIFICATION_FAILED", "targetDate": args.target_date,
            **progress,
            "errorCode": code if re.fullmatch(r"[a-z][a-z0-9_]{1,100}", code) else "archive_recipe_failed",
            "httpStatus": error.status if isinstance(error, ArchiveReadError) else None,
            "cfRay": error.ray if isinstance(error, ArchiveReadError) else None,
        }, separators=(",", ":")), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
