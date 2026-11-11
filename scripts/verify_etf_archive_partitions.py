"""Exact GET-only byte/row verification, not provenance or publication approval.

Existing archive-verify workflow request_json supports either exact object
references, one bounded date-candidate resolution, or one raw-source identity.
The raw-source contract accepts sourceId, tradeDate and rawSha256, reads the
exact metadata and its hash-pinned raw object, and saves verified evidence only
to the workflow's seven-day artifact. It never proves publication readiness.
Exact normalized reads accept at most
eight distinct market-date partitions; date resolution requests one date from
the existing authenticated history-artifact route, which may inspect no more
than two R2 prefixes (128 objects each) and returns at most one object. Both
paths cap response/expanded bytes at two MB, reject redirects, do not retry or
mutate, and export compact receipts only. Candidate selection/hash evidence
does not prove official provenance, calendar completeness, budget availability,
or recovered/published period calculations.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
import zlib

from scripts import build_phase0_archive_recipe as archive_transport
from scripts.build_phase0_archive_recipe import ArchiveReadError, _NoRedirect, _archive_get
from trading_desk.etf_replay import MAX_SOURCE_BYTES, parse_replay_partition

CONTRACT = "flow-etf-partition-verification-v1"
DATE_CANDIDATE_CONTRACT = "flow-etf-date-candidate-resolution-v1"
RAW_SOURCE_CONTRACT = "flow-etf-raw-source-verification-v1"
MAX_PARTITIONS = 8


def _verify_raw_source(value, *, read=None):
    """Resolve one exact metadata identity, then verify its raw bytes (two GETs)."""
    if not isinstance(value, dict) or set(value) != {"contract", "partition"} or value["contract"] != RAW_SOURCE_CONTRACT:
        raise ValueError("etf_raw_source_request_invalid")
    part = value["partition"]
    if not isinstance(part, dict) or set(part) != {"sourceId", "tradeDate", "rawSha256"}:
        raise ValueError("etf_raw_source_identity_invalid")
    source, day, digest = part["sourceId"], part["tradeDate"], part["rawSha256"]
    if source not in ("sse", "szse") or not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("etf_raw_source_identity_invalid")
    date.fromisoformat(day)
    prefix = f"market-data/raw/{source}/etf_scale/{day}/{digest}"
    read = read or (lambda key, sha, metadata: _archive_get(key, sha, metadata_raw=metadata, max_bytes=MAX_SOURCE_BYTES))
    metadata_bytes = read(prefix + ".metadata.json", digest, True)
    if len(metadata_bytes) > MAX_SOURCE_BYTES:
        raise ValueError("etf_raw_source_size_exceeded")
    metadata = json.loads(metadata_bytes)
    identity = metadata.get("source", {})
    key = metadata.get("archived_raw_key")
    if (metadata.get("sha256") != digest or metadata.get("requested_date") != day
            or identity.get("source_id") != source or identity.get("dataset") != "etf_scale"
            or metadata.get("release_id") != f"etf_scale:{source}:{day}:{digest[:16]}"
            or not isinstance(key, str) or not re.fullmatch(re.escape(prefix) + r"\.(json|js|jsonp|xlsx|gz)", key)):
        raise ValueError("etf_raw_source_metadata_mismatch")
    raw = read(key, digest, False)
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("etf_raw_source_bytes_mismatch")
    return {"contract": RAW_SOURCE_CONTRACT, "status": "RAW_SOURCE_BYTES_VERIFIED",
            "partition": part, "objectKey": key, "archiveReadCount": 2, "archiveReadAttempts": 2,
            "rawBytes": len(raw), "metadataSha256": hashlib.sha256(metadata_bytes).hexdigest(),
            "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
            "provesOfficialProvenance": False, "provesPeriodRecovery": False}, metadata_bytes, raw


def verify_raw_source(value, *, read=None):
    completed = 0
    attempts = 0
    def bounded_read(key, digest, metadata):
        nonlocal completed, attempts
        attempts += 1
        if attempts > 2:
            raise ValueError("etf_raw_source_request_limit")
        data = (read(key, digest, metadata) if read else
                _archive_get(key, digest, metadata_raw=metadata, max_bytes=MAX_SOURCE_BYTES))
        completed += 1
        return data
    try:
        return _verify_raw_source(value, read=bounded_read)
    except Exception as error:
        failure = {"errorCode": "etf_raw_source_validation_failed"}
        if isinstance(error, ArchiveReadError):
            failure["errorCode"] = str(error)
            if error.status is not None:
                failure["httpStatus"] = error.status
            if error.ray:
                failure["rayId"] = error.ray
        raise ArchiveVerificationError({
            "contract": RAW_SOURCE_CONTRACT, "status": "RAW_SOURCE_VERIFICATION_FAILED",
            "archiveReadCount": completed, "archiveReadAttempts": attempts,
            "failure": failure,
            "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
            "provesPeriodRecovery": False,
        }) from None


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


def validate_date_candidate_request(value):
    if not isinstance(value, dict) or set(value) != {"contract", "partition"} \
            or value["contract"] != DATE_CANDIDATE_CONTRACT:
        raise ValueError("etf_archive_candidate_request_invalid")
    partition = value["partition"]
    if not isinstance(partition, dict) or set(partition) != {"sourceId", "tradeDate", "expectedSha256"}:
        raise ValueError("etf_archive_candidate_identity_invalid")
    source, day, expected = partition["sourceId"], partition["tradeDate"], partition["expectedSha256"]
    if (not isinstance(source, str) or source not in {"sse", "szse"}
            or not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
        raise ValueError("etf_archive_candidate_identity_invalid")
    date.fromisoformat(day)
    return {"sourceId": source, "tradeDate": day, "expectedSha256": expected}


def _archive_get_date_candidate(source, day, *, max_bytes=MAX_SOURCE_BYTES):
    """Read one date-scoped candidate through the existing bounded admin route."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SOURCE_BYTES:
        raise ValueError("etf_archive_candidate_read_limit_invalid")
    required = ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("archive_recipe_auth_missing")
    query = urlencode({"source": source, "dataset": "etf_scale", "date": day, "file": "data"})
    request = Request(f"{archive_transport.ARCHIVE_ORIGIN}/api/v1/admin/history-artifact?{query}", headers={
        "Authorization": f"Bearer {os.environ['FLOW_INGEST_API_KEY']}",
        "CF-Access-Client-Id": os.environ["FLOW_CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": os.environ["FLOW_CF_ACCESS_CLIENT_SECRET"],
        "User-Agent": archive_transport.ARCHIVE_USER_AGENT,
        "Accept": "application/octet-stream",
    })
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            if response.status != 200:
                raise ArchiveReadError("archive_candidate_remote_http_error", status=response.status,
                                       ray=response.headers.get("cf-ray"))
            headers = {
                "key": response.headers.get("x-flow-normalized-key", ""),
                "sha256": response.headers.get("x-flow-normalized-sha256", ""),
                "tradeDate": response.headers.get("x-flow-trade-date", ""),
                "candidateCount": response.headers.get("x-flow-normalized-candidate-count"),
                "selection": response.headers.get("x-flow-normalized-selection"),
                "contentType": response.headers.get("content-type", ""),
            }
            wire = response.read(max_bytes + 1)
    except HTTPError as error:
        ray = error.headers.get("cf-ray") if error.headers else None
        status = error.code
        error.close()
        raise ArchiveReadError("archive_candidate_remote_http_error", status=status, ray=ray) from None
    except (URLError, TimeoutError):
        raise ArchiveReadError("archive_candidate_remote_transport_error") from None
    if len(wire) > max_bytes:
        raise ValueError("archive_candidate_wire_size_exceeded")
    return headers, wire


def _bounded_gzip_decompress(wire, *, max_bytes=MAX_SOURCE_BYTES):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    for offset in range(0, len(wire), 64 * 1024):
        pending = wire[offset:offset + 64 * 1024]
        while pending:
            allowance = max_bytes + 1 - len(output)
            if allowance <= 0:
                raise ValueError("archive_candidate_expanded_size_exceeded")
            output.extend(decoder.decompress(pending, allowance))
            if len(output) > max_bytes:
                raise ValueError("archive_candidate_expanded_size_exceeded")
            pending = decoder.unconsumed_tail
    if not decoder.eof or decoder.unused_data:
        raise ValueError("archive_candidate_gzip_invalid")
    allowance = max_bytes + 1 - len(output)
    output.extend(decoder.flush(allowance))
    if len(output) > max_bytes:
        raise ValueError("archive_candidate_expanded_size_exceeded")
    return bytes(output)


def resolve_date_candidate(value, *, read=None):
    partition = validate_date_candidate_request(value)
    source, day, expected = partition["sourceId"], partition["tradeDate"], partition["expectedSha256"]
    read = read or (lambda: _archive_get_date_candidate(source, day))
    try:
        headers, wire = read()
        key_pattern = re.compile(
            rf"market-data/normalized/{source}/etf_scale/(?:{re.escape(day)}|{day.replace('-', '')})/[a-f0-9]{{64}}/data\.csv(?:\.gz)?"
        )
        if (not isinstance(headers, dict) or not key_pattern.fullmatch(headers.get("key", ""))
                or headers.get("tradeDate") != day or len(wire) > MAX_SOURCE_BYTES):
            raise ValueError("archive_candidate_response_identity_invalid")
        raw_count, raw_selection = headers.get("candidateCount"), headers.get("selection")
        if raw_count is None and raw_selection is None:
            # An indexed pointer response is also an exact date-scoped result;
            # it does not perform unindexed R2 discovery.
            candidate_count, selection = 1, "indexed-pointer"
        else:
            if (not isinstance(raw_count, str) or not re.fullmatch(r"\d{1,3}", raw_count)
                    or not isinstance(raw_selection, str)):
                raise ValueError("archive_candidate_selection_invalid")
            candidate_count = int(raw_count)
            selection = raw_selection
            if not 1 <= candidate_count <= 256 or selection != ("single" if candidate_count == 1 else "latest-uploaded"):
                raise ValueError("archive_candidate_selection_invalid")
        normalized = (_bounded_gzip_decompress(wire) if headers.get("contentType", "").startswith("application/gzip")
                      or headers["key"].endswith(".csv.gz") else wire)
        actual_sha = hashlib.sha256(normalized).hexdigest()
        declared = headers.get("sha256")
        if declared and not re.fullmatch(r"[a-f0-9]{64}", declared):
            raise ValueError("archive_candidate_declared_hash_invalid")
        frame = parse_replay_partition(normalized, source, day)
        evidence = {
            **partition, "objectKey": headers["key"], "actualSha256": actual_sha,
            "declaredSha256": declared or None, "declaredHashMatchesBytes": actual_sha == declared if declared else None,
            "expectedSha256MatchesBytes": actual_sha == expected,
            "candidateCount": candidate_count, "selection": selection,
            "bytes": len(normalized), "responseBytes": len(wire), "rows": len(frame),
        }
        return {"contract": DATE_CANDIDATE_CONTRACT, "status": "DATE_CANDIDATE_RESOLVED",
                "candidate": evidence, "archiveReadCount": 1,
                "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
                "provesOfficialProvenance": False, "provesExpectedBytes": evidence["expectedSha256MatchesBytes"],
                "provesPeriodRecovery": False}
    except Exception as error:
        failure = {"index": 1, "sourceId": source, "tradeDate": day,
                   "errorCode": str(error) if isinstance(error, ArchiveReadError) else "archive_candidate_validation_failed"}
        if isinstance(error, ArchiveReadError):
            if error.status is not None:
                failure["httpStatus"] = error.status
            if error.ray:
                failure["rayId"] = error.ray
        raise ArchiveVerificationError({
            "contract": DATE_CANDIDATE_CONTRACT, "status": "DATE_CANDIDATE_RESOLUTION_FAILED",
            "partition": partition, "archiveReadCount": 0, "failure": failure,
            "sideEffects": {"remoteWrites": False, "dispatch": False, "activation": False},
            "provesOfficialProvenance": False, "provesExpectedBytes": False, "provesPeriodRecovery": False,
        }) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = os.environ.get("FLOW_ETF_ARCHIVE_REQUEST_JSON", "")
    if len(raw.encode()) > 8192:
        raise ValueError("etf_archive_verification_request_too_large")
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("contract") == RAW_SOURCE_CONTRACT:
            result, metadata, source = verify_raw_source(value)
            evidence_dir = args.output.parent / "raw-source-evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "metadata.json").write_bytes(metadata)
            (evidence_dir / "source.bin").write_bytes(source)
        elif isinstance(value, dict) and value.get("contract") == DATE_CANDIDATE_CONTRACT:
            result = resolve_date_candidate(value)
        else:
            result = verify(value)
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
