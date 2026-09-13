#!/usr/bin/env python3
"""Export a bounded, GET-only legacy provenance preview.

This is an evidence adapter for the deployed
``GET /api/v1/admin/recovery-provenance`` surface.  It never sends a
mutation request, dispatches a workflow, writes D1/R2, or changes a release
pointer.  The output directory is deliberately local and isolated so the
preview can be attached to a review without committing production evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping


SCOPE_START = "2024-08-01"
SCOPE_END = "2026-09-03"
CONTRACT = "recovery-provenance-repair-v1"
PREVIEW_CONTRACT = "recovery-provenance-preview-v1"
SCHEMA_VERSION = 1
MAX_LIMIT = 4
MAX_PAGES = 250
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 60
SHA256 = re.compile(r"^[a-f0-9]{64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "access_key",
    "api_key",
)


class ProvenancePreviewError(ValueError):
    """A bounded preview request or response violated its contract."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        # Details are intentionally not printed or persisted by the CLI.  A
        # caller can use the code to identify the failure without risking a
        # URL, header or provider response leaking into an artifact.
        super().__init__(code if detail is None else f"{code}: {detail}")


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _assert_fixed_scope(start: str, end: str, limit: int) -> None:
    if start != SCOPE_START or end != SCOPE_END:
        raise ProvenancePreviewError("fixed_scope_required")
    if not _valid_date(start) or not _valid_date(end) or end < start:
        raise ProvenancePreviewError("invalid_scope")
    if not isinstance(limit, int) or limit < 1 or limit > MAX_LIMIT:
        raise ProvenancePreviewError("invalid_limit")


def _assert_output_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    parts = [part.lower() for part in resolved.parts]
    if "memory" in parts:
        raise ProvenancePreviewError("protected_output_path")
    for index, part in enumerate(parts[:-1]):
        if part == "reports" and parts[index + 1] == "phase0":
            raise ProvenancePreviewError("protected_output_path")
    return resolved


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contains_secret_key(value: Any, path: str = "response") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SECRET_KEY_PARTS):
                return f"{path}.{key}"
            found = _contains_secret_key(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _contains_secret_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _api_url(base_url: str, start: str, end: str, cursor: str | None, limit: int) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ProvenancePreviewError("invalid_base_url")
    path = parsed.path.rstrip("/") + "/api/v1/admin/recovery-provenance"
    params: list[tuple[str, str]] = [("start", start), ("end", end), ("limit", str(limit))]
    if cursor is not None:
        params.append(("cursor", cursor))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, urllib.parse.urlencode(params), ""))


def _read_bounded(response: Any) -> bytes:
    body = response.read(MAX_PAGE_BYTES + 1)
    if not isinstance(body, (bytes, bytearray)):
        raise ProvenancePreviewError("invalid_http_body")
    if len(body) > MAX_PAGE_BYTES:
        raise ProvenancePreviewError("response_size_exceeded")
    return bytes(body)


def _fetch_page(
    base_url: str,
    api_key: str,
    access_client_id: str | None,
    access_client_secret: str | None,
    start: str,
    end: str,
    cursor: str | None,
    limit: int,
    timeout: int,
    opener: Callable[..., Any],
) -> tuple[Mapping[str, Any], bytes]:
    url = _api_url(base_url, start, end, cursor, limit)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "flow-r0d-provenance-preview/1.0",
    }
    if access_client_id and access_client_secret:
        headers["CF-Access-Client-Id"] = access_client_id
        headers["CF-Access-Client-Secret"] = access_client_secret
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        response = opener(request, timeout=timeout)
        with response:
            status = int(getattr(response, "status", 200))
            body = _read_bounded(response)
    except urllib.error.HTTPError as error:
        # Do not persist the provider body or URL.  A 503 is a transport or
        # storage failure, not an empty evidence page.
        raise ProvenancePreviewError(f"http_{error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProvenancePreviewError("http_request_failed") from None
    if status != 200:
        raise ProvenancePreviewError(f"http_{status}")
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProvenancePreviewError("invalid_json_response") from None
    if not isinstance(parsed, Mapping):
        raise ProvenancePreviewError("invalid_json_response")
    return parsed, body


def _page_request(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProvenancePreviewError("request_contract_missing")
    return value


def _validate_page(
    value: Mapping[str, Any],
    start: str,
    end: str,
    cursor: str | None,
    limit: int,
) -> Mapping[str, Any]:
    if _contains_secret_key(value):
        raise ProvenancePreviewError("secret_field_in_response")
    if value.get("schemaVersion") != SCHEMA_VERSION or value.get("contract") != CONTRACT:
        raise ProvenancePreviewError("contract_mismatch")
    if value.get("scope") != {"start": SCOPE_START, "end": SCOPE_END}:
        raise ProvenancePreviewError("scope_mismatch")
    request = _page_request(value.get("request"))
    expected_request = {"start": start, "end": end, "cursor": cursor, "limit": limit}
    if dict(request) != expected_request:
        raise ProvenancePreviewError("request_mismatch")
    plan_hash = value.get("planHash")
    if not isinstance(plan_hash, str) or not SHA256.fullmatch(plan_hash):
        raise ProvenancePreviewError("plan_hash_invalid")
    if value.get("planStatus") != "NOT_APPROVED" or value.get("executionBlocked") is not True:
        raise ProvenancePreviewError("execution_not_blocked")
    if value.get("reconciliationStatus") != "PENDING":
        raise ProvenancePreviewError("reconciliation_contract_invalid")
    if value.get("budgetStatus") != "unknown":
        raise ProvenancePreviewError("budget_contract_invalid")
    if value.get("status") not in {"READY_TO_APPLY_METADATA_ONLY", "INCOMPLETE", "NO_EVIDENCE"}:
        raise ProvenancePreviewError("status_invalid")
    has_more = value.get("hasMore")
    if not isinstance(has_more, bool):
        raise ProvenancePreviewError("pagination_contract_invalid")
    next_cursor = value.get("nextCursor")
    if next_cursor is not None and not _valid_date(next_cursor):
        raise ProvenancePreviewError("pagination_cursor_invalid")
    if has_more and next_cursor is None:
        raise ProvenancePreviewError("pagination_cursor_missing")
    if cursor is not None and next_cursor is not None and next_cursor <= cursor:
        raise ProvenancePreviewError("pagination_cursor_not_advancing")
    if next_cursor is not None and not (start <= next_cursor <= end):
        raise ProvenancePreviewError("pagination_cursor_out_of_scope")
    d1 = value.get("d1")
    r2 = value.get("r2")
    if not isinstance(d1, Mapping) or not isinstance(r2, Mapping):
        raise ProvenancePreviewError("resource_contract_missing")
    rows_written = d1.get("rowsWritten")
    if rows_written is not None and rows_written != 0:
        raise ProvenancePreviewError("d1_write_reported")
    if r2.get("listCalls") != 0 or r2.get("rawBodiesRead") != 0 or r2.get("normalizedBodiesRead") != 0:
        raise ProvenancePreviewError("r2_readonly_contract_invalid")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_pretty_bytes(value))


def _sum_known(values: list[Any]) -> int | None:
    if not values or any(not isinstance(value, int) or value < 0 for value in values):
        return None
    return sum(values)


def export_preview(
    *,
    base_url: str,
    api_key: str,
    start: str,
    end: str,
    limit: int,
    output_dir: Path,
    timeout: int = 30,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    _assert_fixed_scope(start, end, limit)
    if not api_key:
        raise ProvenancePreviewError("required_read_credentials_missing")
    if not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise ProvenancePreviewError("invalid_timeout")
    if bool(access_client_id) != bool(access_client_secret):
        raise ProvenancePreviewError("access_secret_pair_incomplete")
    output = _assert_output_path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ProvenancePreviewError("output_dir_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    request_opener = opener or urllib.request.urlopen
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    total_bytes = 0
    all_values: list[Mapping[str, Any]] = []

    for page_number in range(1, MAX_PAGES + 1):
        value, raw_body = _fetch_page(
            base_url,
            api_key,
            access_client_id,
            access_client_secret,
            start,
            end,
            cursor,
            limit,
            timeout,
            request_opener,
        )
        _validate_page(value, start, end, cursor, limit)
        total_bytes += len(raw_body)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ProvenancePreviewError("total_response_size_exceeded")
        canonical = _canonical_bytes(value)
        page_path = output / f"page-{page_number:03d}.json"
        _write_json(page_path, value)
        next_cursor = value.get("nextCursor")
        pages.append(
            {
                "page": page_number,
                "cursor": cursor,
                "nextCursor": next_cursor,
                "status": value["status"],
                "planHash": value["planHash"],
                "responseBytes": len(raw_body),
                "responseSha256": _sha256(canonical),
                "file": page_path.name,
            }
        )
        all_values.append(value)
        if not value["hasMore"]:
            break
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
            raise ProvenancePreviewError("pagination_cursor_repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ProvenancePreviewError("page_cap_exceeded")

    statuses = [page["status"] for page in pages]
    preview_status = "READY_TO_APPLY_METADATA_ONLY" if statuses and all(status == "READY_TO_APPLY_METADATA_ONLY" for status in statuses) else "INCOMPLETE"
    d1_values = [value.get("d1", {}).get("rowsRead") for value in all_values]
    d1_written_values = [value.get("d1", {}).get("rowsWritten") for value in all_values]
    r2_gets = [value.get("r2", {}).get("gets") for value in all_values]
    r2_heads = [value.get("r2", {}).get("heads") for value in all_values]
    r2_bytes = [value.get("r2", {}).get("bytesRead") for value in all_values]
    issue_codes = sorted({
        str(item.get("code"))
        for value in all_values
        for item in (value.get("issues") or [])
        if isinstance(item, Mapping) and item.get("code")
    })
    summary: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "contract": PREVIEW_CONTRACT,
        "scope": {"start": SCOPE_START, "end": SCOPE_END},
        "request": {"start": start, "end": end, "limit": limit},
        "status": preview_status,
        "planStatus": "NOT_APPROVED",
        "reconciliationStatus": "PENDING",
        "budgetStatus": "unknown",
        "executionBlocked": True,
        "pageCount": len(pages),
        "nextCursor": None,
        "pages": pages,
        "issueCodes": issue_codes,
        "resourceCounters": {
            "d1Queries": len(pages),
            "d1RowsRead": _sum_known(d1_values),
            "d1RowsWritten": _sum_known(d1_written_values),
            "r2Gets": _sum_known(r2_gets),
            "r2Heads": _sum_known(r2_heads),
            "r2BytesRead": _sum_known(r2_bytes),
            "responseBytes": total_bytes,
        },
        "safety": {
            "httpMethods": ["GET"],
            "d1RowsWritten": _sum_known(d1_written_values),
            "r2Writes": 0,
            "workflowDispatches": 0,
            "activePointerChanges": 0,
        },
    }
    _write_json(output / "provenance-preview.json", summary)
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=MAX_LIMIT)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output: Path | None = None
    write_failure = False
    try:
        output = _assert_output_path(args.output_dir)
        write_failure = not output.exists() or not any(output.iterdir())
        summary = export_preview(
            base_url=args.base_url,
            api_key=os.environ.get("FLOW_INGEST_API_KEY", ""),
            start=args.start,
            end=args.end,
            limit=args.limit,
            output_dir=output,
            timeout=args.timeout,
            access_client_id=os.environ.get("FLOW_CF_ACCESS_CLIENT_ID") or None,
            access_client_secret=os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET") or None,
        )
    except ProvenancePreviewError as error:
        # Keep failure evidence useful but deliberately free of exception
        # details, URLs and secrets.
        if output is None:
            try:
                output = _assert_output_path(args.output_dir)
            except ProvenancePreviewError:
                output = None
        if output is not None and write_failure:
            output.mkdir(parents=True, exist_ok=True)
            _write_json(output / "failure.json", {"schemaVersion": SCHEMA_VERSION, "contract": PREVIEW_CONTRACT, "status": "INCOMPLETE", "executionBlocked": True, "errorCode": error.code})
        print(f"provenance preview failed: {error.code}", file=sys.stderr)
        return 2
    print(json.dumps({"status": summary["status"], "pageCount": summary["pageCount"], "executionBlocked": True}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
