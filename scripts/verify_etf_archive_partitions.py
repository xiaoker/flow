"""Exact GET-only byte/row verification, not provenance or publication approval.

Existing archive-verify workflow request_json accepts {contract, objects}, with
objects containing exact objectKey/sha256 pairs. At most eight distinct market
date partitions, two MB accepted per response; no discovery, list, retry or
mutation. Existing authenticated transport rejects redirects and checks headers.
Only compact receipts are exported, never source contents or credentials. A
matching object proves retained bytes, not official provenance, calendar
completeness, budget availability, or recovered/published period calculations.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re

from scripts.build_phase0_archive_recipe import ArchiveReadError, _archive_get
from trading_desk.etf_replay import MAX_SOURCE_BYTES, parse_replay_partition

CONTRACT = "flow-etf-partition-verification-v1"
MAX_PARTITIONS = 8


class ArchiveVerificationError(ValueError):
    """A bounded read failed; carries only safe, compact evidence."""

    def __init__(self, receipt):
        super().__init__(receipt["failure"]["errorCode"])
        self.receipt = receipt


def validate_request(value):
    if (not isinstance(value, dict) or set(value) != {"contract", "objects"}
            or value["contract"] != CONTRACT or not isinstance(value["objects"], list)
            or not 1 <= len(value["objects"]) <= MAX_PARTITIONS):
        raise ValueError("etf_archive_verification_request_invalid")
    seen = set()
    for row in value["objects"]:
        if not isinstance(row, dict) or set(row) != {"objectKey", "sha256"}:
            raise ValueError("etf_archive_verification_identity_invalid")
        key, digest = row["objectKey"], row["sha256"]
        match = re.fullmatch(r"market-data/normalized/(sse|szse)/etf_scale/(\d{4}-\d{2}-\d{2})/[a-f0-9]{64}/data\.csv", key) if isinstance(key, str) else None
        if not match or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("etf_archive_verification_identity_invalid")
        source, day = match.groups()
        date.fromisoformat(day)
        if (source, day) in seen:
            raise ValueError("etf_archive_verification_duplicate_partition")
        seen.add((source, day))
    return sorted(value["objects"], key=lambda row: row["objectKey"])


def verify(value, *, read=None):
    rows = validate_request(value)  # Validate the whole scope before any GET.
    read = read or (lambda key, digest: _archive_get(key, digest, max_bytes=MAX_SOURCE_BYTES))
    evidence = []
    for index, row in enumerate(rows, start=1):
        key, digest = row["objectKey"], row["sha256"]
        try:
            data = read(key, digest)
            if len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("etf_archive_verification_bytes_mismatch")
            parts = key.split("/")
            frame = parse_replay_partition(data, parts[2], parts[4])
        except Exception as error:
            failure = {"index": index, "objectKey": key,
                       "errorCode": str(error) if isinstance(error, ArchiveReadError)
                       else "archive_object_validation_failed"}
            if isinstance(error, ArchiveReadError):
                if error.status is not None:
                    failure["httpStatus"] = error.status
                if error.ray:
                    failure["rayId"] = error.ray
            raise ArchiveVerificationError({
                "contract": CONTRACT, "status": "ARCHIVE_VERIFICATION_FAILED",
                "objects": evidence, "archiveReadCount": len(evidence),
                "responseBytes": sum(item["bytes"] for item in evidence),
                "failure": failure,
                "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
                "provesOfficialProvenance": False, "provesPeriodRecovery": False,
            }) from None
        evidence.append({**row, "tradeDate": parts[4], "sourceId": parts[2],
                         "bytes": len(data), "rows": len(frame)})
    return {"contract": CONTRACT, "status": "EXACT_ARCHIVE_BYTES_VERIFIED",
            "objects": evidence, "archiveReadCount": len(evidence),
            "responseBytes": sum(row["bytes"] for row in evidence),
            "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
            "provesOfficialProvenance": False, "provesPeriodRecovery": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = os.environ.get("FLOW_ETF_ARCHIVE_REFS_JSON", "")
    if len(raw.encode()) > 8192:
        raise ValueError("etf_archive_verification_request_too_large")
    try:
        result = verify(json.loads(raw))
    except ArchiveVerificationError as error:
        result = error.receipt
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        failure = result["failure"]
        print(json.dumps({"status": result["status"], "archiveReadCount": result["archiveReadCount"],
                          "failure": failure}, sort_keys=True))
        raise SystemExit(1) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "archiveReadCount": result["archiveReadCount"]}))


if __name__ == "__main__":
    main()
