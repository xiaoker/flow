#!/usr/bin/env python3
"""Repair missing SHA-256 metadata on exact, immutable R2 release artifacts.

The script is intentionally a bounded coordinator, not a bulk storage tool.
It consumes a bounded Wrangler D1 JSON export, builds a deterministic plan,
and sends one authenticated request at a time to the feature-flagged Worker
endpoint.  It never lists R2, downloads R2 objects, writes D1, changes a
release pointer, dispatches another workflow, or prints credentials.

``dry-run`` still calls the Worker because the Worker must verify the object
bytes against the D1 manifest.  ``apply`` is fail-closed unless the complete
candidate set fits the explicit object/byte ceilings and the Worker has its
separate write flag enabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import requests


CONTRACT = "r2-release-metadata-repair-v1"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELEASE_ID = re.compile(r"^release:\d{4}-\d{2}-\d{2}:[a-f0-9]{16}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
ARTIFACT_NAME = re.compile(r"^(bootstrap|overview|radar|radar_flow|rotation|national|connect|margin|pressure|stock_search|stock_[0-7])$")
MAX_DATE_SPAN_DAYS = 1_100
MAX_MANIFEST_ROWS = 1_000
MAX_D1_ROWS_READ = 500_000
MAX_OBJECTS = 750
MAX_BYTES = 500_000_000
MAX_OBJECT_BYTES = 8_000_000
REPAIR_INTENT = "release-metadata-sha256-v1"
APPLY_CONFIRMATION = "R2_METADATA_REPAIR_APPLY_20260909"


@dataclass(frozen=True)
class Candidate:
    as_of: str
    release_id: str
    artifact_name: str
    key: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "asOf": self.as_of,
            "releaseId": self.release_id,
            "artifactName": self.artifact_name,
            "key": self.key,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_scope(start: str, end: str) -> None:
    if not _is_date(start) or not _is_date(end):
        raise ValueError("invalid_date_scope")
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise ValueError("reverse_date_scope")
    if (last - first).days + 1 > MAX_DATE_SPAN_DAYS:
        raise ValueError("date_scope_too_large")


def _unwrap_rows(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize Wrangler's one-statement JSON envelope and retain meta."""

    entries: list[Any]
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = [payload]
    else:
        raise ValueError("d1_export_invalid")

    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("meta"), dict):
            meta.update(entry["meta"])
        result = entry.get("results")
        if isinstance(result, list):
            for row in result:
                if not isinstance(row, dict):
                    raise ValueError("d1_row_invalid")
                rows.append(row)
        elif "as_of" in entry and "release_id" in entry:
            rows.append(entry)
    if len(rows) > MAX_MANIFEST_ROWS:
        raise ValueError("d1_manifest_row_limit")
    return rows, meta


def _descriptor(row: dict[str, Any], name: str, start: str, end: str) -> Candidate:
    as_of = row.get("as_of")
    release_id = row.get("release_id")
    if not _is_date(as_of) or as_of < start or as_of > end:
        raise ValueError("manifest_date_invalid")
    if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id) or not release_id.startswith(f"release:{as_of}:"):
        raise ValueError("manifest_release_id_invalid")
    try:
        manifest = json.loads(row["manifest_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_json_invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("asOf") != as_of or manifest.get("releaseId") != release_id:
        raise ValueError("manifest_identity_mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or name not in artifacts or not isinstance(artifacts[name], dict):
        raise ValueError("artifact_not_declared")
    value = artifacts[name]
    key = value.get("key")
    sha256 = value.get("sha256")
    size = value.get("bytes")
    prefix = f"market-data/releases/{as_of}/{release_id}/"
    if not isinstance(key, str) or key != f"{prefix}{name}.json" or ".." in key or "//" in key or any(char in key for char in "\r\n"):
        raise ValueError("artifact_key_invalid")
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        raise ValueError("artifact_sha256_invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_OBJECT_BYTES:
        raise ValueError("artifact_size_invalid")
    return Candidate(as_of, release_id, name, key, sha256, size)


def build_candidates(rows: Iterable[dict[str, Any]], *, start: str, end: str) -> list[Candidate]:
    validate_scope(start, end)
    by_key: dict[str, Candidate] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("d1_row_invalid")
        if row.get("status") not in {"ACTIVE", "SUPERSEDED", "STAGED"}:
            continue
        try:
            manifest = json.loads(row["manifest_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("manifest_json_invalid") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
            raise ValueError("manifest_artifacts_invalid")
        for name in sorted(manifest["artifacts"]):
            if not ARTIFACT_NAME.fullmatch(name):
                continue
            candidate = _descriptor(row, name, start, end)
            previous = by_key.get(candidate.key)
            if previous and (previous.sha256 != candidate.sha256 or previous.bytes != candidate.bytes):
                raise ValueError("artifact_descriptor_conflict")
            by_key[candidate.key] = candidate
    return sorted(by_key.values(), key=lambda item: (item.as_of, item.release_id, item.artifact_name, item.key))


def canonical_plan_hash(
    candidates: Iterable[Candidate], *, start: str, end: str, mode: str, contract: str = CONTRACT
) -> str:
    ordered = sorted(candidates, key=lambda item: (item.as_of, item.release_id, item.artifact_name, item.key))
    payload = {
        "contract": contract,
        "start": start,
        "end": end,
        "mode": mode,
        "candidates": [candidate.as_dict() for candidate in ordered],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def select_candidates(candidates: list[Candidate], *, max_objects: int, max_bytes: int, mode: str) -> tuple[list[Candidate], bool]:
    if isinstance(max_objects, bool) or max_objects <= 0 or max_objects > MAX_OBJECTS:
        raise ValueError("max_objects_invalid")
    if isinstance(max_bytes, bool) or max_bytes <= 0 or max_bytes > MAX_BYTES:
        raise ValueError("max_bytes_invalid")
    selected: list[Candidate] = []
    total_bytes = 0
    for candidate in candidates:
        if len(selected) >= max_objects or total_bytes + candidate.bytes > max_bytes:
            break
        selected.append(candidate)
        total_bytes += candidate.bytes
    truncated = len(selected) != len(candidates)
    if truncated and mode == "apply":
        raise ValueError("apply_selection_incomplete")
    return selected, truncated


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing_environment:{name}")
    return value


def _measured_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def repair_one(
    session: requests.Session,
    *,
    base_url: str,
    candidate: Candidate,
    mode: str,
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {_required_env('FLOW_INGEST_API_KEY')}",
        "Content-Type": "application/json",
        "x-flow-r2-repair-intent": REPAIR_INTENT,
    }
    access_id = os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", "").strip()
    access_secret = os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", "").strip()
    if bool(access_id) != bool(access_secret):
        raise RuntimeError("access_secret_pair_incomplete")
    if access_id:
        headers["CF-Access-Client-Id"] = access_id
        headers["CF-Access-Client-Secret"] = access_secret
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/admin/r2-release-metadata-repair",
        headers=headers,
        json={
            "asOf": candidate.as_of,
            "releaseId": candidate.release_id,
            "artifactName": candidate.artifact_name,
            "mode": mode,
        },
        timeout=timeout,
        allow_redirects=False,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"error": "invalid_worker_response"}
    if not response.ok:
        code = body.get("error") if isinstance(body, dict) else "repair_failed"
        return {"candidate": candidate.as_dict(), "status": "error", "httpStatus": response.status_code, "error": str(code)}
    if not isinstance(body, dict) or body.get("asOf") != candidate.as_of or body.get("releaseId") != candidate.release_id or body.get("artifactName") != candidate.artifact_name:
        return {"candidate": candidate.as_dict(), "status": "error", "httpStatus": response.status_code, "error": "worker_identity_mismatch"}
    status = str(body.get("status", "unknown"))
    if status not in {"already_verified", "verified_missing_metadata", "repaired"}:
        return {"candidate": candidate.as_dict(), "status": "error", "httpStatus": response.status_code, "error": "worker_status_invalid"}
    return {"candidate": candidate.as_dict(), "status": status, "httpStatus": response.status_code, "worker": body}


def build_report(
    *,
    payload: Any,
    start: str,
    end: str,
    mode: str,
    max_objects: int,
    max_bytes: int,
) -> tuple[dict[str, Any], list[Candidate]]:
    rows, meta = _unwrap_rows(payload)
    candidates = build_candidates(rows, start=start, end=end)
    selected, truncated = select_candidates(candidates, max_objects=max_objects, max_bytes=max_bytes, mode=mode)
    rows_read = meta.get("rows_read")
    rows_written = meta.get("rows_written")
    if isinstance(rows_read, bool) or not isinstance(rows_read, int) or rows_read < 0:
        rows_read = None
    if isinstance(rows_written, bool) or not isinstance(rows_written, int) or rows_written < 0:
        rows_written = None
    report = {
        "schemaVersion": 1,
        "contract": CONTRACT,
        "status": "READY_TO_EXECUTE" if not truncated else "TRUNCATED_DRY_RUN",
        "mode": mode,
        "scope": {"start": start, "end": end},
        "candidateCount": len(candidates),
        "selectedCount": len(selected),
        "selectedBytes": sum(candidate.bytes for candidate in selected),
        "truncated": truncated,
        "planHash": canonical_plan_hash(selected, start=start, end=end, mode=mode),
        "fullPlanHash": canonical_plan_hash(candidates, start=start, end=end, mode=mode),
        "d1RowsRead": rows_read,
        "d1RowsWritten": rows_written,
        "d1QueryRowsRead": rows_read,
        "d1QueryRowsWritten": rows_written,
        "r2ClassAUpperBound": len(selected) if mode == "apply" else 0,
        "r2ClassBUpperBound": len(selected) * (3 if mode == "apply" else 2),
        "r2GetBytesUpperBound": sum(candidate.bytes for candidate in selected),
        "r2PutBytesUpperBound": sum(candidate.bytes for candidate in selected) if mode == "apply" else 0,
        "workerRequestsUpperBound": len(selected),
        "candidates": [candidate.as_dict() for candidate in selected],
        "results": [],
        "actual": {
            "measurementStatus": "PENDING",
            "workerRequests": 0,
            "d1RowsRead": 0,
            "d1RowsWritten": 0,
            "r2ClassA": 0,
            "r2ClassB": 0,
            "r2GetBytes": 0,
            "r2PutBytes": 0,
        },
    }
    if rows_written not in (0, None):
        report["status"] = "BLOCKED_READONLY_VIOLATION"
    if rows_read is None or rows_written is None:
        report["status"] = "INCOMPLETE_BUDGET_UNKNOWN"
    elif rows_read > MAX_D1_ROWS_READ:
        report["status"] = "BLOCKED_D1_READ_CAP"
    return report, selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded R2 release metadata repair coordinator")
    parser.add_argument("--input", type=Path, required=True, help="Bounded Wrangler D1 JSON export")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", ""))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--mode", choices=("dry-run", "apply"), default="dry-run")
    parser.add_argument("--max-objects", type=int, default=1)
    parser.add_argument("--max-bytes", type=int, default=10_000_000)
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate_scope(args.start, args.end)
    if args.mode == "apply" and args.confirm_apply != APPLY_CONFIRMATION:
        raise SystemExit("apply_confirmation_required")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report, selected = build_report(
        payload=payload,
        start=args.start,
        end=args.end,
        mode=args.mode,
        max_objects=args.max_objects,
        max_bytes=args.max_bytes,
    )
    if report["status"] in {"BLOCKED_READONLY_VIOLATION", "INCOMPLETE_BUDGET_UNKNOWN", "BLOCKED_D1_READ_CAP"}:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "planHash": report["planHash"]}, ensure_ascii=False))
        return 2
    if args.mode == "apply" and not args.base_url:
        raise SystemExit("base_url_required")
    session = requests.Session()
    for candidate in selected:
        result = repair_one(session, base_url=args.base_url, candidate=candidate, mode=args.mode, timeout=45.0)
        report["results"].append(result)
        if result.get("status") == "error":
            report["status"] = "INCOMPLETE_REPAIR_ERROR"
            break
        worker = result.get("worker") if isinstance(result.get("worker"), dict) else {}
        actual = report["actual"]
        actual["workerRequests"] += 1
        for source, target in (("d1RowsRead", "d1RowsRead"), ("d1RowsWritten", "d1RowsWritten"), ("r2ClassA", "r2ClassA"), ("r2ClassB", "r2ClassB")):
            value = _measured_int(worker.get(source))
            if value is not None:
                actual[target] += value
            else:
                actual["measurementStatus"] = "INCOMPLETE"
        actual_bytes = _measured_int(worker.get("actualBytes"))
        if actual_bytes is None:
            actual["measurementStatus"] = "INCOMPLETE"
        elif result.get("status") == "verified_missing_metadata":
            actual["r2GetBytes"] += actual_bytes
        elif result.get("status") == "repaired":
            actual["r2GetBytes"] += actual_bytes
            actual["r2PutBytes"] += actual_bytes
    else:
        report["status"] = "VERIFIED" if args.mode == "dry-run" else "REPAIRED"
        if report["actual"]["measurementStatus"] == "PENDING":
            report["actual"]["measurementStatus"] = "MEASURED"
    if report["status"] == "INCOMPLETE_REPAIR_ERROR":
        report["actual"]["measurementStatus"] = "INCOMPLETE"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "planHash": report["planHash"], "selectedCount": report["selectedCount"]}, ensure_ascii=False))
    return 0 if report["status"] in {"VERIFIED", "REPAIRED"} else 2


if __name__ == "__main__":
    sys.exit(main())
