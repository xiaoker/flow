"""Read-only, bounded construction of an exact Phase 0 archive input candidate.

Live verification is opt-in.  It uses only the existing authenticated archive
GET endpoint; this command never registers a plan or writes production state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trading_desk.phase0_archive import MAX_FILES, MAX_OBJECT_BYTES
from trading_desk.phase0_archive_recipe import build_archive_recipe

ARCHIVE_ORIGIN = "https://flow.xiaoker.com"
MAX_BUNDLE_BYTES = 40_000_000


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


def _archive_get(key: str, digest: str) -> bytes:
    required = ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("archive_recipe_auth_missing")
    query = urlencode({"archive_key": key, "archive_sha256": digest})
    request = Request(f"{ARCHIVE_ORIGIN}/api/v1/admin/history-artifact?{query}", headers={
        "Authorization": f"Bearer {os.environ['FLOW_INGEST_API_KEY']}",
        "CF-Access-Client-Id": os.environ["FLOW_CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["FLOW_CF_ACCESS_CLIENT_SECRET"],
    })
    with build_opener(_NoRedirect).open(request, timeout=20) as response:
        if (response.status != 200 or response.headers.get("x-flow-archive-key") != key
                or response.headers.get("x-flow-archive-sha256") != digest):
            raise ValueError("archive_recipe_remote_identity_mismatch")
        content = response.read(MAX_OBJECT_BYTES + 1)
    if len(content) > MAX_OBJECT_BYTES or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("archive_recipe_remote_hash_mismatch")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--history-bundle", type=Path, required=True)
    parser.add_argument("--official-calendar", type=Path, required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--verify-live-archive", action="store_true")
    parser.add_argument("--adjacent-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = _read_json(args.manifest)
    adjacent_objects = {}
    adjacent = manifest.get("archivedEtfInputs", [])
    if adjacent:
        if len(adjacent) != 1 or not isinstance(adjacent[0], dict):
            raise ValueError("archive_recipe_adjacent_invalid")
        ref = adjacent[0]
        if args.verify_live_archive:
            adjacent_objects[ref["objectKey"]] = _archive_get(ref["objectKey"], ref["sha256"])
        elif args.adjacent_file is not None:
            adjacent_objects[ref["objectKey"]] = args.adjacent_file.read_bytes()
        else:
            raise ValueError("archive_recipe_adjacent_object_required")
    recipe, objects = build_archive_recipe(
        manifest=manifest, history_bundle=_read_json(args.history_bundle),
        calendar=_read_json(args.official_calendar), target=args.target_date,
        adjacent_objects=adjacent_objects)
    if args.verify_live_archive:
        if len(recipe["files"]) > MAX_FILES:
            raise ValueError("archive_recipe_file_limit_exceeded")
        for ref in recipe["files"]:
            remote = adjacent_objects.get(ref["objectKey"])
            if remote is None:
                remote = _archive_get(ref["objectKey"], ref["sha256"])
            if remote != objects[ref["objectKey"]]:
                raise ValueError("archive_recipe_remote_content_mismatch")
    args.output.write_text(json.dumps({
        "status": "ARCHIVE_OBJECTS_VERIFIED" if args.verify_live_archive else "UNVERIFIED_CANDIDATE",
        "targetDate": args.target_date,
        "archiveReadCount": len(recipe["files"]) if args.verify_live_archive else 0,
        "recipe": recipe,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
