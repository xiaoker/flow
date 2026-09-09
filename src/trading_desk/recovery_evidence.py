"""Bounded, read-only exporter for the R0-D reconciliation evidence bundle.

The exporter is deliberately an adapter, not a recovery runner.  It can only
issue authenticated GET requests to the PR B inventory surface and can only
write an isolated local evidence artifact.  D1/R2 writes, workflow dispatch,
release publication, flag changes and pointer changes are not represented by
this module's capabilities.

The public evidence contract is ``evidence-bundle-v2``.  The existing offline
auditor still consumes its version-1 input contract; :func:`to_audit_input`
performs that explicit, lossless adapter conversion so the auditor remains the
single owner of matrix, plan and stage semantics.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .recovery_audit import RecoveryAuditError, audit_bundle, write_audit_outputs
from .recovery_execution import assert_recovery_output_path, canonical_sha256


EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_CONTRACT = "evidence-bundle-v2"
INVENTORY_SCHEMA_VERSION = 1
INVENTORY_CONTRACT = "recovery-inventory-v1"
SCOPE_START = "2024-08-01"
SCOPE_END = "2026-09-03"
CAMPAIGN_ID = "r0d-reconciliation-through-20260903"
PRIORITY_DATES = (
    "2026-08-27",
    "2026-08-28",
    "2026-08-31",
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
)
DEFAULT_LIMIT = 100
MAX_PAGES = 250
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DATES_PER_PAGE = 100
MAX_R2_OBJECTS = 20_000
MAX_R2_PAGE_SIZE = MAX_R2_OBJECTS
MAX_R2_HEADS_PER_REQUEST = 64
# The inventory endpoint is intentionally conservative: even a complete
# manifest surface is bounded before we spend Class-B operations or Worker
# requests.  2,496 is the largest multiple of the 64-head request cap that
# stays below the documented 2,500-head campaign ceiling.
MAX_R2_TOTAL_HEADS = 2_496
MAX_R2_PAGES_PER_DATE_PAGE = MAX_R2_TOTAL_HEADS // MAX_R2_HEADS_PER_REQUEST
MAX_TOTAL_REQUESTS = 300
MAX_R2_CURSOR_LENGTH = 256
# Failure evidence is bounded independently from the response-size guard.  A
# failed R2 HEAD page may contain many date records, but the exporter only
# needs enough structured receipts to identify the first bounded set of bad
# objects without copying the full response into the error record.
MAX_R2_FAILURE_ISSUES = 64
MAX_R2_FAILURE_KEY_LENGTH = 1024
USAGE_MAX_AGE = timedelta(minutes=60)
# These warnings are safe to carry while an R2 HEAD cursor is being drained.
# Any other reason remains fatal so a 503 or incomplete response cannot be
# mistaken for a usable evidence page.
NON_FATAL_PAGINATED_REASONS = frozenset({
    "R2_HEAD_PAGE_REMAINS",
    "RELEASE_MANIFEST_INCOMPLETE",
})
# These reasons describe a bounded response that carries date-scoped evidence
# but is not strong enough to be called COMPLETE.  They must remain visible to
# the offline auditor instead of being mistaken for an HTTP transport failure.
# Reasons outside these explicit sets fail closed so a newly introduced server
# condition cannot silently change the evidence contract.
SEMANTIC_INCOMPLETE_REASONS = frozenset({
    "D1_QUERY_LIMIT",
    "D1_ROWS_READ_LIMIT",
    "D1_ROWS_READ_TELEMETRY_UNKNOWN",
    "D1_ROWS_WRITTEN_TELEMETRY_UNKNOWN",
    "D1_READONLY_VIOLATION",
    "NO_OBSERVED_DATES",
    "R2_CANDIDATE_LIMIT",
    "R2_CURSOR_NOT_IN_INDEX",
    "R2_DUPLICATE_KEY_CONFLICT",
    "R2_MANIFEST_EVIDENCE_MISSING",
    "R2_OBJECT_MISSING",
    "RELEASE_ARTIFACT_KEY_OR_HASH_INVALID",
    "RELEASE_MANIFEST_HASH_MISSING",
    "RELEASE_MANIFEST_INCOMPLETE",
    "RELEASE_MANIFEST_INVALID",
    "SOURCE_UNAVAILABLE",
})
TRANSPORT_INCOMPLETE_REASONS = frozenset({
    "STORAGE_UNAVAILABLE",
    "D1_READ_FAILED",
    "D1_OR_R2_READ_FAILED",
    "R2_HEAD_FAILED",
    "R2_HEAD_FORBIDDEN",
    "R2_HEAD_RATE_LIMITED",
    "R2_HEAD_TIMEOUT",
})
KNOWN_INCOMPLETE_REASONS = (
    SEMANTIC_INCOMPLETE_REASONS
    | NON_FATAL_PAGINATED_REASONS
    | TRANSPORT_INCOMPLETE_REASONS
)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
    "access_key",
    "api_key",
)


class RecoveryEvidenceError(ValueError):
    """A bounded evidence request or response violated its contract."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.evidence = dict(evidence) if evidence is not None else None
        super().__init__(code if not detail else f"{code}: {detail}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _require_scope(start: str, end: str, priority_dates: Sequence[str]) -> list[str]:
    if start != SCOPE_START or end != SCOPE_END:
        raise RecoveryEvidenceError("fixed_scope_required", f"{start}..{end}")
    if not _valid_date(start) or not _valid_date(end) or end < start:
        raise RecoveryEvidenceError("invalid_scope")
    priority = sorted(set(priority_dates))
    if any(not _valid_date(item) or item < start or item > end for item in priority):
        raise RecoveryEvidenceError("priority_date_outside_scope")
    return priority


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contains_secret_key(value: Any, path: str = "bundle") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            text = str(key).lower()
            if any(part in text for part in SECRET_KEY_PARTS):
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


def _redacted_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _cursor_date(value: str | None) -> str | None:
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        return None
    try:
        padded = value.replace("-", "+").replace("_", "/") + "==="[: (4 - len(value) % 4) % 4]
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(decoded, Mapping) or decoded.get("v") != 1:
        return None
    candidate = decoded.get("date")
    return candidate if _valid_date(candidate) else None


def _r2_cursor(value: str | None, *, start: str, end: str) -> tuple[str, str] | None:
    if not value or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_R2_CURSOR_LENGTH}}}", value):
        return None
    try:
        padded = value.replace("-", "+").replace("_", "/") + "==="[: (4 - len(value) % 4) % 4]
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(decoded, Mapping) or decoded.get("v") != 1:
        return None
    if decoded.get("start") != start or decoded.get("end") != end:
        return None
    key = decoded.get("key")
    if not isinstance(key, str) or not key.startswith("market-data/releases/") or ".." in key or "//" in key or "\n" in key or len(key) > 512:
        return None
    return value, key


def _bounded_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if any(ord(character) < 0x20 for character in value):
        return None
    return value


def _r2_failure_evidence(body: Mapping[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    """Extract bounded, non-secret receipts from a failed inventory page.

    The inventory route already returns per-artifact HEAD results.  When a
    page is rejected (for example because a 64-head page still has a
    ``R2_HASH_MISSING`` item), the old exporter discarded that response and
    kept only the reason string.  Preserve enough deterministic metadata to
    identify the bad object while never copying object bodies, opaque cursors,
    credentials, or an unbounded response subtree.
    """

    output: dict[str, Any] = {"reasons": sorted(set(str(reason) for reason in reasons))}
    pagination = body.get("r2Pagination")
    if isinstance(pagination, Mapping):
        pagination_evidence: dict[str, Any] = {}
        index_sha = _hash_from_any(pagination.get("indexSha256"))
        if index_sha:
            pagination_evidence["indexSha256"] = index_sha
        for field in ("pageSize", "candidateCount"):
            value = _safe_nonnegative(pagination.get(field))
            if value is not None:
                pagination_evidence[field] = value
        if isinstance(pagination.get("hasMore"), bool):
            pagination_evidence["hasMore"] = pagination["hasMore"]
        if pagination_evidence:
            output["r2Pagination"] = pagination_evidence

    bad_statuses = {"HASH_MISSING", "HASH_MISMATCH", "MISSING", "INCOMPLETE"}
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    dates = body.get("dates")
    if isinstance(dates, list):
        for date_record in dates[:MAX_DATES_PER_PAGE]:
            if not isinstance(date_record, Mapping):
                continue
            data_date = date_record.get("dataDate")
            if not _valid_date(data_date):
                continue
            releases = date_record.get("releases")
            if not isinstance(releases, Mapping):
                continue
            for slot, values in releases.items():
                if slot not in {"dashboard", "etf_penetration"} or not isinstance(values, list):
                    continue
                for release in values:
                    if not isinstance(release, Mapping):
                        continue
                    release_id = _bounded_text(release.get("releaseId"), max_length=512)
                    as_of = release.get("asOf")
                    if release_id is None or not _valid_date(as_of):
                        continue
                    artifacts = release.get("artifacts")
                    if not isinstance(artifacts, Mapping):
                        continue
                    for name, artifact in artifacts.items():
                        if len(issues) >= MAX_R2_FAILURE_ISSUES:
                            break
                        if not isinstance(name, str) or not name or len(name) > 128 or not isinstance(artifact, Mapping):
                            continue
                        status = artifact.get("status")
                        if status not in bad_statuses:
                            continue
                        identity = (str(as_of), release_id, name)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        issue: dict[str, Any] = {
                            "dataDate": str(as_of),
                            "releaseId": release_id,
                            "slot": slot,
                            "artifactName": name,
                            "status": status,
                        }
                        key = _bounded_text(artifact.get("key"), max_length=MAX_R2_FAILURE_KEY_LENGTH)
                        if key and key.startswith("market-data/releases/") and ".." not in key and "//" not in key:
                            issue["key"] = key
                        for source_field, output_field in (
                            ("declaredSha256", "expectedSha256"),
                            ("actualSha256", "actualSha256"),
                        ):
                            value = _hash_from_any(artifact.get(source_field))
                            if value:
                                issue[output_field] = value
                        for source_field, output_field in (
                            ("declaredBytes", "expectedBytes"),
                            ("actualBytes", "actualBytes"),
                        ):
                            value = _safe_nonnegative(artifact.get(source_field))
                            if value is not None:
                                issue[output_field] = value
                        error = _bounded_text(artifact.get("error"), max_length=128)
                        if error:
                            issue["error"] = error
                        issues.append(issue)
                    if len(issues) >= MAX_R2_FAILURE_ISSUES:
                        break
                if len(issues) >= MAX_R2_FAILURE_ISSUES:
                    break
            if len(issues) >= MAX_R2_FAILURE_ISSUES:
                break
    issues.sort(key=lambda item: (item["dataDate"], item["releaseId"], item["artifactName"]))
    if issues:
        output["issues"] = issues
    return output


def _safe_nonnegative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _metric_unknown(reason: str, measured_at: str | None = None) -> dict[str, Any]:
    return {"value": "unknown", "measuredAt": measured_at, "reason": reason}


def _date_from_any(value: Any) -> str | None:
    return value if _valid_date(value) else None


def _hash_from_any(value: Any) -> str | None:
    return value if isinstance(value, str) and SHA256.fullmatch(value) else None


def _first_value(mappings: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Any:
    for mapping in mappings:
        for key in keys:
            value = mapping.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_mapping(values: Any) -> Mapping[str, Any] | None:
    if not isinstance(values, list):
        return None
    return next((item for item in values if isinstance(item, Mapping)), None)


@dataclass(frozen=True)
class InventoryPage:
    body: Mapping[str, Any]
    request_cursor: str | None
    response_sha256: str
    response_bytes: int
    captured_at: str


@dataclass(frozen=True)
class R2InventoryPage:
    body: Mapping[str, Any]
    request_cursor: str | None
    response_sha256: str
    response_bytes: int
    captured_at: str


@dataclass(frozen=True)
class ExportResult:
    bundle: dict[str, Any]
    errors: tuple[dict[str, Any], ...]


Transport = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_PAGE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(MAX_PAGE_BYTES + 1)
    except (TimeoutError, socket.timeout) as error:
        raise RecoveryEvidenceError("inventory_timeout") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise RecoveryEvidenceError("inventory_timeout") from error
        raise RecoveryEvidenceError("inventory_request_failed", type(error).__name__) from error
    except OSError as error:
        raise RecoveryEvidenceError("inventory_request_failed", type(error).__name__) from error


class RecoveryEvidenceExporter:
    """Fetch and normalize the bounded PR B evidence surface."""

    def __init__(
        self,
        *,
        base_url: str,
        start: str = SCOPE_START,
        end: str = SCOPE_END,
        priority_dates: Sequence[str] = PRIORITY_DATES,
        ingest_api_key: str | None = None,
        access_client_id: str | None = None,
        access_client_secret: str | None = None,
        transport: Transport | None = None,
        timeout: float = 25.0,
        limit: int = DEFAULT_LIMIT,
        captured_at: str | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RecoveryEvidenceError("invalid_base_url")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= DEFAULT_LIMIT:
            raise RecoveryEvidenceError("invalid_limit")
        self.base_url = base_url.rstrip("/")
        self.start = start
        self.end = end
        self.priority_dates = _require_scope(start, end, priority_dates)
        self.ingest_api_key = ingest_api_key
        self.access_client_id = access_client_id
        self.access_client_secret = access_client_secret
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self.limit = limit
        self.captured_at = captured_at or _utc_now()
        self.request_count = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "flow-controlled-recovery/2"}
        if self.ingest_api_key:
            headers["Authorization"] = f"Bearer {self.ingest_api_key}"
        if self.access_client_id:
            headers["CF-Access-Client-Id"] = self.access_client_id
        if self.access_client_secret:
            headers["CF-Access-Client-Secret"] = self.access_client_secret
        return headers

    def _url(self, cursor: str | None, r2_cursor: str | None = None) -> str:
        params: list[tuple[str, str]] = [
            ("start", self.start),
            ("end", self.end),
            ("limit", str(self.limit)),
        ]
        if cursor:
            params.append(("cursor", cursor))
        if r2_cursor:
            params.append(("r2Cursor", r2_cursor))
        return f"{self.base_url}/api/v1/admin/recovery-inventory?{urllib.parse.urlencode(params)}"

    def _validate_page(
        self,
        *,
        status_code: int,
        payload: bytes,
        request_cursor: str | None,
        request_r2_cursor: str | None,
    ) -> InventoryPage:
        if len(payload) > MAX_PAGE_BYTES:
            raise RecoveryEvidenceError("response_bytes_exceed_limit")
        if status_code in {401, 403}:
            raise RecoveryEvidenceError("inventory_permission_denied")
        if status_code == 429:
            raise RecoveryEvidenceError("inventory_rate_limited")
        if status_code < 200 or status_code >= 300:
            # The inventory route deliberately returns 503 while a bounded R2
            # page remains.  This is not a complete page and is accepted only
            # for the single explicit continuation reason below.  The body is
            # parsed first so a generic 503 can never masquerade as a page.
            if status_code != 503:
                raise RecoveryEvidenceError("inventory_http_error", str(status_code))
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecoveryEvidenceError("inventory_invalid_json") from error
        if not isinstance(body, Mapping):
            raise RecoveryEvidenceError("inventory_response_not_object")
        if body.get("schemaVersion") != INVENTORY_SCHEMA_VERSION or body.get("contract") != INVENTORY_CONTRACT:
            raise RecoveryEvidenceError("inventory_contract_mismatch")
        request = body.get("request")
        if not isinstance(request, Mapping):
            raise RecoveryEvidenceError("inventory_request_contract_missing")
        expected_request_cursor = request_cursor
        if request.get("start") != self.start or request.get("end") != self.end:
            raise RecoveryEvidenceError("inventory_scope_mismatch")
        if request.get("limit") != self.limit or request.get("cursor") != expected_request_cursor or request.get("r2Cursor") != request_r2_cursor:
            raise RecoveryEvidenceError("inventory_cursor_contract_mismatch")
        status_value = body.get("status")
        if status_value not in {"COMPLETE", "INCOMPLETE"}:
            raise RecoveryEvidenceError("inventory_status_invalid")
        raw_reasons = body.get("incompleteReasons")
        if raw_reasons is None:
            reasons: list[str] = []
        elif not isinstance(raw_reasons, list) or any(not isinstance(item, str) for item in raw_reasons):
            raise RecoveryEvidenceError("inventory_incomplete_reasons_invalid")
        else:
            reasons = list(raw_reasons)
        unknown_reasons = sorted(set(reasons) - KNOWN_INCOMPLETE_REASONS)
        transport_reasons = sorted(set(reasons) & TRANSPORT_INCOMPLETE_REASONS)
        semantic_incomplete = (
            status_value == "INCOMPLETE"
            and bool(reasons)
            and not unknown_reasons
            and not transport_reasons
            and set(reasons).issubset(SEMANTIC_INCOMPLETE_REASONS | NON_FATAL_PAGINATED_REASONS)
        )
        deferred_r2 = (
            status_value == "INCOMPLETE"
            and "R2_HEAD_PAGE_REMAINS" in reasons
            and set(reasons).issubset(NON_FATAL_PAGINATED_REASONS)
        )
        failure_evidence = (
            _r2_failure_evidence(body, reasons)
            if any(reason.startswith("R2_") for reason in reasons)
            else None
        )
        if unknown_reasons or transport_reasons:
            fatal_reasons = unknown_reasons + transport_reasons
            code = "inventory_http_error" if status_code == 503 else "inventory_incomplete"
            detail = ",".join(fatal_reasons) or "incomplete_reason"
            raise RecoveryEvidenceError(code, detail, evidence=failure_evidence)
        # R2_HEAD_PAGE_REMAINS is only non-fatal while its continuation cursor
        # is explicitly being drained.  A terminal COMPLETE page must never
        # claim that pagination is still pending.
        if status_value == "COMPLETE" and reasons:
            raise RecoveryEvidenceError("inventory_incomplete", ",".join(sorted(set(reasons))))
        if status_code == 503 and not deferred_r2 and not semantic_incomplete:
            raise RecoveryEvidenceError(
                "inventory_http_error",
                ",".join(reasons) or "503",
                evidence=failure_evidence,
            )
        if status_value == "INCOMPLETE" and not deferred_r2 and not semantic_incomplete:
            raise RecoveryEvidenceError("inventory_incomplete", ",".join(reasons) or "missing_reason")
        dates = body.get("dates")
        if not isinstance(dates, list) or len(dates) > MAX_DATES_PER_PAGE:
            raise RecoveryEvidenceError("inventory_dates_exceed_limit")
        seen: list[str] = []
        for item in dates:
            if not isinstance(item, Mapping) or not _valid_date(item.get("dataDate")):
                raise RecoveryEvidenceError("inventory_date_invalid")
            seen.append(str(item["dataDate"]))
        if not seen:
            raise RecoveryEvidenceError("inventory_empty_page")
        if seen != sorted(seen) or len(set(seen)) != len(seen):
            raise RecoveryEvidenceError("inventory_dates_not_strictly_ascending")
        has_more = body.get("hasMore")
        next_cursor = body.get("nextCursor")
        if not isinstance(has_more, bool):
            raise RecoveryEvidenceError("inventory_has_more_invalid")
        if has_more:
            if not isinstance(next_cursor, str) or not _cursor_date(next_cursor):
                raise RecoveryEvidenceError("inventory_next_cursor_missing")
            if not seen or _cursor_date(next_cursor) != seen[-1]:
                raise RecoveryEvidenceError("inventory_cursor_not_advanced")
        elif next_cursor not in (None, ""):
            raise RecoveryEvidenceError("inventory_unexpected_next_cursor")
        r2_pagination = body.get("r2Pagination")
        if not isinstance(r2_pagination, Mapping):
            raise RecoveryEvidenceError("inventory_r2_pagination_missing")
        if r2_pagination.get("cursor") != request_r2_cursor:
            raise RecoveryEvidenceError("inventory_r2_cursor_contract_mismatch")
        r2_has_more = r2_pagination.get("hasMore")
        r2_next_cursor = r2_pagination.get("nextCursor")
        if not isinstance(r2_has_more, bool):
            raise RecoveryEvidenceError("inventory_r2_has_more_invalid")
        if deferred_r2 and not r2_has_more:
            raise RecoveryEvidenceError("inventory_r2_deferred_without_next")
        if not deferred_r2 and r2_has_more:
            raise RecoveryEvidenceError("inventory_r2_complete_with_next")
        if r2_has_more:
            if not isinstance(r2_next_cursor, str) or _r2_cursor(r2_next_cursor, start=self.start, end=self.end) is None:
                raise RecoveryEvidenceError("inventory_r2_next_cursor_missing")
        elif r2_next_cursor not in (None, ""):
            raise RecoveryEvidenceError("inventory_r2_unexpected_next_cursor")
        r2_page_size = _safe_nonnegative(r2_pagination.get("pageSize"))
        r2_candidate_count = _safe_nonnegative(r2_pagination.get("candidateCount"))
        if r2_page_size is None or r2_candidate_count is None or r2_page_size > MAX_R2_HEADS_PER_REQUEST or r2_candidate_count > MAX_R2_OBJECTS:
            raise RecoveryEvidenceError("inventory_r2_pagination_invalid")
        if r2_has_more and r2_page_size != MAX_R2_HEADS_PER_REQUEST:
            raise RecoveryEvidenceError("inventory_r2_page_size_not_full")
        index_sha = r2_pagination.get("indexSha256")
        if not isinstance(index_sha, str) or not SHA256.fullmatch(index_sha):
            raise RecoveryEvidenceError("inventory_r2_index_sha_invalid")
        usage = body.get("usage")
        d1 = usage.get("d1") if isinstance(usage, Mapping) and isinstance(usage.get("d1"), Mapping) else None
        if d1 is None or d1.get("rowsReadKnown") is not True or d1.get("rowsWrittenKnown") is not True:
            raise RecoveryEvidenceError("inventory_d1_telemetry_unknown")
        if _safe_nonnegative(d1.get("queryCount")) is None:
            raise RecoveryEvidenceError("inventory_d1_telemetry_unknown")
        written = _safe_nonnegative(d1.get("rowsWritten"))
        if written is None:
            raise RecoveryEvidenceError("inventory_d1_telemetry_unknown")
        if written != 0:
            raise RecoveryEvidenceError("inventory_readonly_violation")
        if isinstance(usage, Mapping) and usage.get("truncated") is True:
            raise RecoveryEvidenceError("inventory_usage_truncated")
        r2 = usage.get("r2") if isinstance(usage, Mapping) and isinstance(usage.get("r2"), Mapping) else None
        if r2 is None:
            raise RecoveryEvidenceError("inventory_r2_telemetry_unknown")
        for field in ("classA", "classB", "headCount", "headLimit"):
            if _safe_nonnegative(r2.get(field)) is None:
                raise RecoveryEvidenceError("inventory_r2_telemetry_unknown")
        if _safe_nonnegative(r2.get("headCount")) > MAX_R2_HEADS_PER_REQUEST or _safe_nonnegative(r2.get("headLimit")) != MAX_R2_HEADS_PER_REQUEST:
            raise RecoveryEvidenceError("inventory_r2_head_limit_invalid")
        return InventoryPage(
            body=body,
            request_cursor=request_cursor,
            response_sha256=_sha_bytes(payload),
            response_bytes=len(payload),
            captured_at=self.captured_at,
        )

    def fetch_pages(self) -> tuple[list[InventoryPage], list[dict[str, str]], list[InventoryPage]]:
        """Fetch date pages while draining each page's bounded R2 HEAD cursor."""

        def merge_r2_artifacts(target: dict[str, Any], source: Mapping[str, Any]) -> None:
            target_dates = {str(item.get("dataDate")): item for item in target.get("dates", []) if isinstance(item, Mapping)}
            source_date_values = source.get("dates") if isinstance(source.get("dates"), list) else []
            for source_date in source_date_values:
                if not isinstance(source_date, Mapping):
                    continue
                target_date = target_dates.get(str(source_date.get("dataDate")))
                if not isinstance(target_date, dict):
                    continue
                target_releases = target_date.get("releases") if isinstance(target_date.get("releases"), dict) else {}
                source_releases = source_date.get("releases") if isinstance(source_date.get("releases"), Mapping) else {}
                for slot, source_values in source_releases.items():
                    if not isinstance(source_values, list):
                        continue
                    target_values = target_releases.setdefault(slot, [])
                    by_release = {str(item.get("releaseId")): item for item in target_values if isinstance(item, dict)}
                    for source_release in source_values:
                        if not isinstance(source_release, Mapping):
                            continue
                        release_id = str(source_release.get("releaseId"))
                        target_release = by_release.get(release_id)
                        if not isinstance(target_release, dict):
                            target_values.append(copy.deepcopy(dict(source_release)))
                            by_release[release_id] = target_values[-1]
                            continue
                        source_artifacts = source_release.get("artifacts") if isinstance(source_release.get("artifacts"), Mapping) else {}
                        target_artifacts = target_release.setdefault("artifacts", {})
                        for name, artifact in source_artifacts.items():
                            if isinstance(artifact, Mapping):
                                target_artifacts[name] = copy.deepcopy(dict(artifact))

        def refresh_stage_status(target: dict[str, Any]) -> None:
            date_values = target.get("dates") if isinstance(target.get("dates"), list) else []
            for date_record in date_values:
                if not isinstance(date_record, dict):
                    continue
                releases = date_record.get("releases") if isinstance(date_record.get("releases"), Mapping) else {}
                ready: dict[str, bool] = {}
                for slot in ("dashboard", "etf_penetration"):
                    values = releases.get(slot) if isinstance(releases.get(slot), list) else []
                    ready[slot] = any(
                        isinstance(item, Mapping)
                        and item.get("status") in {"ACTIVE", "SUPERSEDED"}
                        and item.get("manifestValid") is True
                        and item.get("manifestComplete") is True
                        and item.get("manifestHashStatus") == "READY"
                        and isinstance(item.get("artifacts"), Mapping)
                        and bool(item["artifacts"])
                        and all(isinstance(artifact, Mapping) and artifact.get("status") == "READY" for artifact in item["artifacts"].values())
                        for item in values
                    )
                domain_values = date_record.get("domains") if isinstance(date_record.get("domains"), list) else []
                for domain in domain_values:
                    if not isinstance(domain, dict):
                        continue
                    stages = domain.get("stageStatus") if isinstance(domain.get("stageStatus"), dict) else {}
                    for slot in ("dashboard", "etf_penetration"):
                        values = releases.get(slot) if isinstance(releases.get(slot), list) else []
                        stages[slot] = "READY" if ready[slot] else "INCOMPLETE" if values else "UNKNOWN"

        pages: list[InventoryPage] = []
        transport_pages: list[InventoryPage] = []
        errors: list[dict[str, Any]] = []
        date_cursor_token: str | None = None
        date_cursor: str | None = None
        seen_dates: set[str] = set()
        seen_date_page_keys: set[str] = set()
        total_bytes = 0
        for _ in range(MAX_PAGES):
            aggregate_body: dict[str, Any] | None = None
            r2_cursor_token: str | None = None
            r2_cursor_key: str | None = None
            r2_index_sha: str | None = None
            final_page: InventoryPage | None = None
            seen_r2_hashes: set[str] = set()
            r2_head_total = 0
            for _ in range(MAX_R2_PAGES_PER_DATE_PAGE):
                if self.request_count >= MAX_TOTAL_REQUESTS:
                    errors.append({"code": "inventory_request_limit", "detail": str(MAX_TOTAL_REQUESTS)})
                    break
                url = self._url(date_cursor_token, r2_cursor_token)
                try:
                    self.request_count += 1
                    status_code, payload = self.transport(url, self._headers(), self.timeout)
                    total_bytes += len(payload)
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise RecoveryEvidenceError("response_total_bytes_exceed_limit")
                    page = self._validate_page(
                        status_code=status_code,
                        payload=payload,
                        request_cursor=date_cursor,
                        request_r2_cursor=r2_cursor_token,
                    )
                except RecoveryEvidenceError as error:
                    item: dict[str, Any] = {"code": error.code, "detail": str(error)}
                    if error.evidence is not None:
                        item["evidence"] = copy.deepcopy(error.evidence)
                    errors.append(item)
                    break
                transport_pages.append(page)
                page_usage = page.body.get("usage") if isinstance(page.body.get("usage"), Mapping) else {}
                page_r2_usage = page_usage.get("r2") if isinstance(page_usage.get("r2"), Mapping) else {}
                page_head_count = _safe_nonnegative(page_r2_usage.get("headCount")) or 0
                r2_head_total += page_head_count
                if r2_head_total > MAX_R2_TOTAL_HEADS:
                    errors.append({"code": "inventory_r2_head_limit", "detail": str(MAX_R2_TOTAL_HEADS)})
                    break
                if page.response_sha256 in seen_r2_hashes:
                    errors.append({"code": "inventory_repeated_page", "detail": page.response_sha256})
                    break
                seen_r2_hashes.add(page.response_sha256)
                body = copy.deepcopy(dict(page.body))
                pagination = body["r2Pagination"]
                page_index_sha = str(pagination["indexSha256"])
                if r2_index_sha is None:
                    r2_index_sha = page_index_sha
                elif page_index_sha != r2_index_sha:
                    errors.append({"code": "inventory_r2_index_changed", "detail": page_index_sha})
                    break
                if aggregate_body is None:
                    aggregate_body = body
                else:
                    merge_r2_artifacts(aggregate_body, body)
                if not pagination["hasMore"]:
                    if aggregate_body is None:
                        errors.append({"code": "inventory_empty_page", "detail": "r2_aggregate"})
                        break
                    aggregate_body["incompleteReasons"] = [
                        reason for reason in aggregate_body.get("incompleteReasons", [])
                        if reason != "R2_HEAD_PAGE_REMAINS"
                    ]
                    # R2 pagination is the only condition resolved by draining
                    # this inner cursor.  Preserve every other semantic
                    # warning so the logical page remains auditable as
                    # INCOMPLETE rather than being upgraded by the exporter.
                    aggregate_body["status"] = (
                        "INCOMPLETE" if aggregate_body["incompleteReasons"] else "COMPLETE"
                    )
                    aggregate_body["request"] = {**aggregate_body.get("request", {}), "r2Cursor": None}
                    aggregate_body["r2Pagination"] = {
                        **pagination,
                        "cursor": None,
                        "nextCursor": None,
                        "hasMore": False,
                        "indexSha256": r2_index_sha,
                    }
                    refresh_stage_status(aggregate_body)
                    aggregate_payload = _json_bytes(aggregate_body)
                    final_page = InventoryPage(
                        body=aggregate_body,
                        request_cursor=date_cursor,
                        response_sha256=_sha_bytes(aggregate_payload),
                        response_bytes=len(aggregate_payload),
                        captured_at=self.captured_at,
                    )
                    break
                if r2_head_total >= MAX_R2_TOTAL_HEADS:
                    errors.append({"code": "inventory_r2_head_limit", "detail": str(MAX_R2_TOTAL_HEADS)})
                    break
                next_token = pagination.get("nextCursor")
                decoded_next = _r2_cursor(str(next_token), start=self.start, end=self.end) if isinstance(next_token, str) else None
                if decoded_next is None:
                    errors.append({"code": "inventory_r2_next_cursor_missing", "detail": str(next_token)})
                    break
                next_key = decoded_next[1]
                if r2_cursor_key and next_key <= r2_cursor_key:
                    errors.append({"code": "inventory_r2_cursor_loop", "detail": next_key})
                    break
                r2_cursor_token, r2_cursor_key = decoded_next
            else:
                errors.append({"code": "inventory_r2_page_limit", "detail": str(MAX_R2_PAGES_PER_DATE_PAGE)})

            if errors or final_page is None:
                break
            page_dates = [str(item["dataDate"]) for item in final_page.body["dates"]]
            page_key = canonical_sha256({
                "dates": page_dates,
                "hasMore": final_page.body["hasMore"],
                "nextCursor": final_page.body["nextCursor"],
                "r2IndexSha256": final_page.body["r2Pagination"]["indexSha256"],
            })
            if page_key in seen_date_page_keys:
                errors.append({"code": "inventory_repeated_page", "detail": page_key})
                break
            seen_date_page_keys.add(page_key)
            if seen_dates.intersection(page_dates):
                errors.append({"code": "inventory_duplicate_date", "detail": ",".join(page_dates)})
                break
            seen_dates.update(page_dates)
            pages.append(final_page)
            if not final_page.body["hasMore"]:
                return pages, errors, transport_pages
            next_cursor = str(final_page.body["nextCursor"])
            next_date = _cursor_date(next_cursor)
            if not next_date or (date_cursor and next_date <= date_cursor):
                errors.append({"code": "inventory_cursor_loop", "detail": next_cursor})
                break
            date_cursor_token = next_cursor
            date_cursor = next_date
        else:
            errors.append({"code": "inventory_page_limit", "detail": str(MAX_PAGES)})
        return pages, errors, transport_pages

    def collect(self) -> ExportResult:
        pages, errors, transport_pages = self.fetch_pages()
        bundle = build_evidence_bundle(
            pages=pages,
            start=self.start,
            end=self.end,
            priority_dates=self.priority_dates,
            captured_at=self.captured_at,
            errors=errors,
            request_count=self.request_count,
            transport_pages=transport_pages,
        )
        return ExportResult(bundle=bundle, errors=tuple(errors))


def _domain_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _domain_applicability(
    domain: Mapping[str, Any],
    readiness: Sequence[Mapping[str, Any]],
) -> str:
    explicit = domain.get("applicability")
    if isinstance(explicit, str) and explicit:
        return explicit.upper()
    applicable = [item for item in readiness if isinstance(item, Mapping)]
    required = [item for item in applicable if item.get("required") is True]
    if required:
        return "REQUIRED"
    closed = [str(item.get("status", "")).upper() for item in applicable]
    if closed and all(item in {"OFFICIAL_CLOSED", "NOT_APPLICABLE", "CLOSED"} for item in closed):
        return "OFFICIAL_CLOSED" if "OFFICIAL_CLOSED" in closed else "NOT_APPLICABLE"
    return "UNKNOWN"


def _domain_row(
    data_date: str,
    domain: Mapping[str, Any],
    readiness: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    domain_name = _domain_id(domain.get("domain")) or ""
    stage_status = domain.get("stageStatus") if isinstance(domain.get("stageStatus"), Mapping) else {}
    evidence = domain.get("evidence") if isinstance(domain.get("evidence"), Mapping) else {}
    raw_items = evidence.get("raw") if isinstance(evidence.get("raw"), list) else []
    normalized_items = evidence.get("normalized") if isinstance(evidence.get("normalized"), list) else []
    pcf_items = evidence.get("pcfExact") if isinstance(evidence.get("pcfExact"), list) else []
    raw = _first_mapping(raw_items) or {}
    normalized = _first_mapping(normalized_items) or {}
    pcf = _first_mapping(pcf_items) or {}
    matching_readiness = [
        item for item in readiness
        if isinstance(item, Mapping)
        and item.get("sourceId")
        and _domain_for_source(item.get("sourceId")) == domain_name
    ]
    readiness_item = _first_mapping(matching_readiness) or {}
    raw_nested = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    readiness_nested = readiness_item.get("evidence") if isinstance(readiness_item.get("evidence"), Mapping) else {}
    provenance_sources = (raw, raw_nested, readiness_item, readiness_nested)
    raw_key = _first_value(provenance_sources, ("objectKey", "rawObjectKey"))
    raw_key = raw_key if isinstance(raw_key, str) else None
    raw_sha = _hash_from_any(_first_value(provenance_sources, ("sha256", "rawSha256")))
    normalized_key = normalized.get("objectKey")
    normalized_key = normalized_key if isinstance(normalized_key, str) else None
    normalized_sha = _hash_from_any(normalized.get("sha256"))
    declared_raw_sha = _hash_from_any(_first_value((raw, raw_nested), ("declaredSha256", "declaredRawSha256")))
    computed_raw_sha = _hash_from_any(_first_value((raw, raw_nested), ("computedSha256", "computedRawSha256")))
    declared_normalized_sha = _hash_from_any(_first_value((normalized,), ("declaredSha256", "declaredNormalizedSha256")))
    computed_normalized_sha = _hash_from_any(_first_value((normalized,), ("computedSha256", "computedNormalizedSha256")))
    pcf_date = _date_from_any(_first_value((pcf,), ("pcfDate", "tradeDate")))
    source_date = _date_from_any(_first_value(provenance_sources, ("sourceBusinessDate", "businessDate", "targetDate")))
    fingerprint = _hash_from_any(_first_value(provenance_sources, ("sourceFingerprint", "source_fingerprint")))
    row_count = 0
    for item in (*raw_items, *normalized_items, *pcf_items):
        if isinstance(item, Mapping) and isinstance(item.get("rowCount"), int) and item["rowCount"] >= 0:
            row_count = max(row_count, int(item["rowCount"]))
    gap_reason = _first_value(provenance_sources, ("reason", "gapReason")) or domain.get("gapReason")
    stages = {stage: stage_status.get(stage, "UNKNOWN") for stage in (
        "raw", "normalized", "model", "pcf_exact", "dashboard", "etf_penetration", "date_visible"
    )}
    return {
        "dataDate": data_date,
        "domain": domain_name,
        "market": domain.get("market"),
        "required": bool(domain.get("required", True)),
        "applicability": _domain_applicability(domain, matching_readiness),
        "stageStatus": stages,
        "rawKey": raw_key,
        "rawSha256": raw_sha if isinstance(raw_sha, str) else None,
        "declaredRawSha256": declared_raw_sha if isinstance(declared_raw_sha, str) else None,
        "computedRawSha256": computed_raw_sha if isinstance(computed_raw_sha, str) else None,
        "normalizedKey": normalized_key,
        "normalizedSha256": normalized_sha if isinstance(normalized_sha, str) else None,
        "declaredNormalizedSha256": declared_normalized_sha if isinstance(declared_normalized_sha, str) else None,
        "computedNormalizedSha256": computed_normalized_sha if isinstance(computed_normalized_sha, str) else None,
        "hashOrigin": _first_value(provenance_sources, ("hashOrigin", "hash_origin")),
        "sourceBusinessDate": source_date,
        "publicationDate": _date_from_any(_first_value(provenance_sources, ("publicationDate", "publication_date"))),
        "navDate": _date_from_any(_first_value(provenance_sources, ("navDate", "nav_date"))),
        "pcfDate": pcf_date,
        "effectiveDate": _date_from_any(_first_value((domain, raw, raw_nested), ("effectiveDate", "effective_date"))),
        "rows": row_count,
        "unit": domain.get("unit"),
        "schemaVersion": domain.get("schemaVersion"),
        "calculationVersion": domain.get("calculationVersion"),
        "sourceFingerprint": fingerprint,
        "hashStatus": _first_value((raw, raw_nested, normalized), ("hashStatus", "hash_status")),
        "hashMismatch": bool(
            _first_value((raw, raw_nested), ("hashStatus", "hash_status")) in {"HASH_MISMATCH", "MISMATCH"}
            or _first_value((normalized,), ("hashStatus", "hash_status")) in {"HASH_MISMATCH", "MISMATCH"}
            or (isinstance(declared_raw_sha, str) and isinstance(computed_raw_sha, str) and declared_raw_sha != computed_raw_sha)
            or (isinstance(declared_normalized_sha, str) and isinstance(computed_normalized_sha, str) and declared_normalized_sha != computed_normalized_sha)
        ),
        "runKeys": [
            item.get("runKey") for item in (domain.get("pipelineWindows") or [])
            if isinstance(item, Mapping) and isinstance(item.get("runKey"), str)
        ],
        "releaseIds": [],
        "slots": [stage for stage, status in stages.items() if status in {"READY", "COMPLETE", "COMPLETED"}],
        "gapReason": gap_reason if isinstance(gap_reason, str) else None,
        "evidence": {
            "marketHistory": bool(domain.get("marketHistory")),
            "historicalSnapshot": bool(evidence.get("dateVisible")),
            "dashboardRelease": bool(evidence.get("dashboard")),
            "penetrationRelease": bool(evidence.get("etfPenetration")),
            "sourceEvidence": bool(raw_items or matching_readiness),
            "sourceReadiness": matching_readiness,
        },
    }


def _domain_for_source(source_id: Any) -> str | None:
    return {
        "stock_connect_summary": "hkex_connect",
        "sse_etf": "sse_etf",
        "sse_etf_nav": "sse_etf",
        "sse_etf_pcf": "sse_etf",
        "szse_etf": "szse_etf",
        "sse_margin": "sse_margin",
        "szse_margin": "szse_margin",
        "bse_margin": "bse_margin",
    }.get(source_id)


def _release_rows(date_record: Mapping[str, Any]) -> list[dict[str, Any]]:
    releases = date_record.get("releases")
    if not isinstance(releases, Mapping):
        return []
    output: list[dict[str, Any]] = []
    for slot, values in releases.items():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping) or not _valid_date(item.get("asOf")):
                continue
            artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), Mapping) else {}
            artifacts_ready = bool(artifacts) and all(
                isinstance(artifact, Mapping) and artifact.get("status") == "READY"
                for artifact in artifacts.values()
            )
            manifest_sha = _hash_from_any(item.get("manifestSha256"))
            release_id = item.get("releaseId") if isinstance(item.get("releaseId"), str) else None
            release_slot = slot if slot in {"dashboard", "etf_penetration"} else item.get("slot", "dashboard")
            if release_slot not in {"dashboard", "etf_penetration"}:
                release_slot = "dashboard"
            release_status = item.get("status") if isinstance(item.get("status"), str) else None
            complete = bool(
                item.get("manifestComplete") is True
                and item.get("manifestHashStatus") == "READY"
                and manifest_sha
                and artifacts_ready
            )
            output.append({
                "releaseId": release_id,
                "asOf": item.get("asOf"),
                "slot": release_slot,
                "complete": complete,
                "status": release_status,
                "manifestSha256": manifest_sha,
            })
    return output


def _r2_rows(release_rows: Sequence[Mapping[str, Any]], dates: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    objects: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for date_record in dates:
        releases = date_record.get("releases") if isinstance(date_record.get("releases"), Mapping) else {}
        for values in releases.values():
            if not isinstance(values, list):
                continue
            for release in values:
                if not isinstance(release, Mapping):
                    continue
                artifacts = release.get("artifacts") if isinstance(release.get("artifacts"), Mapping) else {}
                for name, artifact in artifacts.items():
                    if not isinstance(artifact, Mapping):
                        continue
                    key = artifact.get("key")
                    actual_sha = artifact.get("actualSha256")
                    if artifact.get("status") != "READY" or not isinstance(key, str) or not SHA256.fullmatch(str(actual_sha or "")):
                        gaps.append(f"{release.get('asOf')}:{release.get('releaseId')}:{name}")
                        continue
                    # A READY artifact must carry the byte count returned by
                    # the bounded R2 HEAD.  Falling back to the manifest's
                    # declared size would turn an existence-only receipt into
                    # false proof that the stored object still matches.
                    actual_bytes = _safe_nonnegative(artifact.get("actualBytes"))
                    if actual_bytes is None:
                        gaps.append(f"{release.get('asOf')}:{release.get('releaseId')}:{name}:bytes")
                        continue
                    objects[key] = {
                        "key": key,
                        "sha256": str(actual_sha),
                        "dataDate": release.get("asOf"),
                        "domain": "release",
                        "stage": release.get("slot"),
                        "bytes": actual_bytes,
                    }
    if len(objects) > MAX_R2_OBJECTS:
        gaps.append("r2_object_limit")
    ordered = sorted(objects.values(), key=lambda item: item["key"])
    if len(ordered) > MAX_R2_PAGE_SIZE:
        gaps.append("r2_object_page_limit")
    return ordered[:MAX_R2_PAGE_SIZE], sorted(set(gaps))


def _usage_summary(
    pages: Sequence[InventoryPage],
    *,
    request_count: int | None = None,
    transport_pages: Sequence[InventoryPage] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    measured_pages = list(transport_pages if transport_pages is not None else pages)
    rows_read = 0
    rows_written = 0
    rows_read_known = True
    rows_written_known = True
    class_a = class_b = heads = 0
    for page in measured_pages:
        usage = page.body.get("usage") if isinstance(page.body.get("usage"), Mapping) else {}
        d1 = usage.get("d1") if isinstance(usage.get("d1"), Mapping) else {}
        read = _safe_nonnegative(d1.get("rowsRead"))
        written = _safe_nonnegative(d1.get("rowsWritten"))
        if read is None or d1.get("rowsReadKnown") is False:
            rows_read_known = False
        else:
            rows_read += read
        if written is None or d1.get("rowsWrittenKnown") is False:
            rows_written_known = False
        else:
            rows_written += written
        r2 = usage.get("r2") if isinstance(usage.get("r2"), Mapping) else {}
        class_a += _safe_nonnegative(r2.get("classA")) or 0
        class_b += _safe_nonnegative(r2.get("classB")) or 0
        heads += _safe_nonnegative(r2.get("headCount")) or 0
    requests = len(measured_pages) if request_count is None else request_count
    complete_page_usage = bool(measured_pages) and requests == len(measured_pages)
    d1_audit = {
        "rowsRead": rows_read if rows_read_known and complete_page_usage else "unknown",
        "rowsWritten": rows_written if rows_written_known and complete_page_usage else "unknown",
        "workersRequests": requests,
    }
    usage = {
        "d1RowsRead": rows_read if rows_read_known and measured_pages else "unknown",
        "d1RowsWritten": rows_written if rows_written_known and measured_pages else "unknown",
        "workerRequests": requests,
        "r2ClassA": class_a,
        "r2ClassB": class_b,
        "r2HeadCount": heads,
        "pages": len(measured_pages),
        "metricsMeasuredAt": measured_pages[-1].captured_at if measured_pages else None,
    }
    return d1_audit, usage


def _page_usage(page: InventoryPage) -> dict[str, Any]:
    usage = page.body.get("usage") if isinstance(page.body.get("usage"), Mapping) else {}
    d1 = usage.get("d1") if isinstance(usage.get("d1"), Mapping) else {}
    r2 = usage.get("r2") if isinstance(usage.get("r2"), Mapping) else {}
    return {
        "d1QueryCount": _safe_nonnegative(d1.get("queryCount")),
        "d1RowsRead": _safe_nonnegative(d1.get("rowsRead")),
        "d1RowsWritten": _safe_nonnegative(d1.get("rowsWritten")),
        "d1RowsReadKnown": d1.get("rowsReadKnown") is True,
        "d1RowsWrittenKnown": d1.get("rowsWrittenKnown") is True,
        "r2ClassA": _safe_nonnegative(r2.get("classA")),
        "r2ClassB": _safe_nonnegative(r2.get("classB")),
        "r2HeadCount": _safe_nonnegative(r2.get("headCount")),
        "r2HeadLimit": _safe_nonnegative(r2.get("headLimit")),
        "truncated": usage.get("truncated") is True,
    }


def _active_release_slots(pages: Sequence[InventoryPage]) -> list[dict[str, Any]]:
    values: dict[tuple[str, str], dict[str, Any]] = {}
    for page in pages:
        raw = page.body.get("activeReleaseSlots")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            slot = item.get("slot")
            release_id = item.get("releaseId")
            as_of = item.get("asOf")
            if slot not in {"dashboard", "etf_penetration"} or not isinstance(release_id, str) or not _valid_date(as_of):
                continue
            manifest_sha = _hash_from_any(item.get("manifestSha256"))
            values[(str(slot), release_id)] = {
                "slot": slot,
                "releaseId": release_id,
                "asOf": as_of,
                "manifestSha256": manifest_sha,
                "observedAt": item.get("observedAt") if isinstance(item.get("observedAt"), str) else None,
                "activatedAt": item.get("activatedAt") if isinstance(item.get("activatedAt"), str) else None,
            }
    return sorted(values.values(), key=lambda item: (item["slot"], item["releaseId"]))[:2]


def _unknown_telemetry(reason: str, measured_at: str | None) -> dict[str, dict[str, Any]]:
    return {field: _metric_unknown(reason, measured_at) for field in (
        "d1RowsReadPerDay", "d1RowsWrittenPerDay", "workersRequestsPerDay",
        "githubActionsMinutes", "r2StorageBytes", "r2ClassA", "r2ClassB",
    )}


def load_official_usage(path: str | Path | None, *, now: datetime | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load a hashed, timestamped official usage export; otherwise stay unknown."""

    measured_at = None
    if path is None:
        return _unknown_telemetry("official_usage_not_provided", measured_at), {"status": "UNKNOWN", "reason": "not_provided"}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _unknown_telemetry("official_usage_unreadable", measured_at), {"status": "UNKNOWN", "reason": "unreadable"}
    secret_path = _contains_secret_key(payload)
    if secret_path:
        return _unknown_telemetry("official_usage_secret_field_rejected", None), {"status": "UNKNOWN", "reason": "secret_field_rejected"}
    if not isinstance(payload, Mapping):
        return _unknown_telemetry("official_usage_not_object", None), {"status": "UNKNOWN", "reason": "not_object"}
    captured = payload.get("capturedAt")
    measured_at = captured if isinstance(captured, str) else None
    expected_hash = payload.get("sha256")
    unsigned = {key: value for key, value in payload.items() if key != "sha256"}
    if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash) or expected_hash != canonical_sha256(unsigned):
        return _unknown_telemetry("official_usage_hash_mismatch", measured_at), {"status": "UNKNOWN", "reason": "hash_mismatch"}
    try:
        captured_at = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except ValueError:
        return _unknown_telemetry("official_usage_timestamp_invalid", measured_at), {"status": "UNKNOWN", "reason": "timestamp_invalid"}
    current = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    if captured_at > current + timedelta(minutes=5):
        return _unknown_telemetry("official_usage_timestamp_in_future", measured_at), {"status": "UNKNOWN", "reason": "timestamp_in_future"}
    expires = payload.get("expiresAt")
    try:
        expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00")) if expires else None
    except ValueError:
        expires_at = current - timedelta(seconds=1)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if current - captured_at > USAGE_MAX_AGE or (expires_at and current > expires_at):
        return _unknown_telemetry("official_usage_expired", measured_at), {"status": "UNKNOWN", "reason": "expired", "sha256": expected_hash}
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return _unknown_telemetry("official_usage_metrics_missing", measured_at), {"status": "UNKNOWN", "reason": "metrics_missing", "sha256": expected_hash}
    fields = ("d1RowsReadPerDay", "d1RowsWrittenPerDay", "workersRequestsPerDay", "githubActionsMinutes", "r2StorageBytes", "r2ClassA", "r2ClassB")
    output: dict[str, dict[str, Any]] = {}
    for field in fields:
        value = metrics.get(field)
        valid_metric = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        output[field] = {"value": value, "measuredAt": measured_at, "source": "official_export"} if valid_metric else _metric_unknown("metric_missing", measured_at)
    if any(item["value"] == "unknown" for item in output.values()):
        status = "UNKNOWN"
    else:
        status = "VALID"
    return output, {
        "status": status,
        "reason": "verified" if status == "VALID" else "metrics_incomplete",
        "capturedAt": measured_at,
        "sha256": expected_hash,
    }


def build_evidence_bundle(
    *,
    pages: Sequence[InventoryPage],
    start: str,
    end: str,
    priority_dates: Sequence[str],
    captured_at: str,
    errors: Sequence[Mapping[str, Any]],
    request_count: int | None = None,
    transport_pages: Sequence[InventoryPage] | None = None,
    official_usage: Mapping[str, Mapping[str, Any]] | None = None,
    usage_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    priority = _require_scope(start, end, priority_dates)
    dates: list[Mapping[str, Any]] = [
        item
        for page in pages
        for item in (page.body.get("dates") or [])
        if isinstance(item, Mapping)
    ]
    inventory: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []
    pipelines: list[dict[str, Any]] = []
    for date_record in dates:
        data_date = date_record.get("dataDate")
        if not _valid_date(data_date):
            continue
        readiness = date_record.get("sourceReadiness") if isinstance(date_record.get("sourceReadiness"), list) else []
        domains = date_record.get("domains") if isinstance(date_record.get("domains"), list) else []
        for domain in domains:
            if isinstance(domain, Mapping):
                inventory.append(_domain_row(str(data_date), domain, readiness))
        releases.extend(_release_rows(date_record))
        for item in date_record.get("pipelineWindows") or []:
            if isinstance(item, Mapping):
                pipelines.append({
                    "runKey": item.get("runKey", item.get("run_key")),
                    "pipeline": item.get("pipeline"),
                    "windowDate": item.get("windowDate", item.get("window_date")),
                    "windowName": item.get("windowName", item.get("window_name")),
                    "targetDate": item.get("targetDate", item.get("target_date")),
                    "status": item.get("status"),
                    "attemptCount": item.get("attemptCount", item.get("attempt_count", 0)),
                    "lastErrorCode": item.get("lastErrorCode", item.get("last_error_code")),
                })
    r2_objects, r2_gaps = _r2_rows(releases, dates)
    if not releases or not any(
        isinstance(date_record.get("releases"), Mapping)
        and any(isinstance(values, list) and values for values in date_record["releases"].values())
        for date_record in dates
    ):
        r2_gaps.append("r2_manifest_evidence_missing")
        r2_gaps = sorted(set(r2_gaps))
    r2_index_payload = {"objects": r2_objects, "gaps": r2_gaps, "source": "release-manifest-head-receipts"}
    r2_index_sha = next(
        (
            str(page.body["r2Pagination"]["indexSha256"])
            for page in pages
            if isinstance(page.body.get("r2Pagination"), Mapping)
            and isinstance(page.body["r2Pagination"].get("indexSha256"), str)
        ),
        canonical_sha256(r2_index_payload),
    )
    d1_audit, usage = _usage_summary(pages, request_count=request_count, transport_pages=transport_pages)
    telemetry = dict(official_usage or _unknown_telemetry("official_usage_not_provided", captured_at))
    source_receipts = [
        {
            "kind": "recovery-inventory",
            "requestCursor": page.request_cursor,
            "responseSha256": page.response_sha256,
            "responseBytes": page.response_bytes,
            "capturedAt": page.captured_at,
            "dateCount": len(page.body.get("dates") or []),
            "hasMore": page.body.get("hasMore"),
            "nextCursor": page.body.get("nextCursor"),
            "usage": _page_usage(page),
        }
        for page in pages
    ]
    status = "COMPLETE" if pages and not errors else "INCOMPLETE"
    bundle: dict[str, Any] = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "contract": EVIDENCE_CONTRACT,
        # Capture time is evidence metadata, not plan identity.  Keeping the
        # campaign identifier fixed makes replayed fixtures yield the same
        # canonical planHash across runs.
        "campaignId": CAMPAIGN_ID,
        "capturedAt": captured_at,
        "exportStatus": status,
        "errors": [dict(item) for item in errors],
        "scope": {"startDate": start, "endDateInclusive": end, "priorityDates": priority},
        "pages": source_receipts,
        "activeReleaseSlots": _active_release_slots(pages),
        "inventory": inventory,
        "releaseManifests": releases,
        "pipelineWindows": pipelines,
        "r2Objects": r2_objects,
        "r2Inventory": {
            "page": 0,
            "pageSize": len(r2_objects),
            "hardLimit": MAX_R2_PAGE_SIZE,
            "headHardLimit": MAX_R2_TOTAL_HEADS,
            "hasMore": bool(errors) or "r2_object_page_limit" in r2_gaps,
            "indexSha256": r2_index_sha,
            "suspiciousGaps": r2_gaps,
            "source": "release-manifest-head-receipts",
        },
        "accountTelemetry": telemetry,
        "accountTelemetryEvidence": dict(usage_status or {"status": "UNKNOWN", "reason": "not_provided"}),
        "d1Audit": d1_audit,
        "runtimeEvidence": {
            "metricsMeasuredAt": captured_at,
            "inventoryTransportPages": len(transport_pages if transport_pages is not None else pages),
            "r2HeadPageCount": sum(
                1 for page in (transport_pages if transport_pages is not None else pages)
                if isinstance(page.body.get("r2Pagination"), Mapping)
            ),
            "repository": {
                "originMain": os.environ.get("RECOVERY_ORIGIN_MAIN_SHA", "unknown"),
                "headSha": os.environ.get("GITHUB_SHA", "unknown"),
            },
            "readOnlyAssertions": {
                "d1Writes": 0 if d1_audit["rowsWritten"] == 0 else "unknown",
                "r2Writes": 0,
                "workflowDispatches": 0,
                "activePointerChanges": 0,
                "flagChanges": 0,
            },
            "resourceUsage": usage,
        },
        "readOnlyAssertions": {
            "d1Writes": 0 if d1_audit["rowsWritten"] == 0 else "unknown",
            "r2Writes": 0,
            "workflowDispatches": 0,
            "activePointerChanges": 0,
            "flagChanges": 0,
        },
        "protectedPathsTouched": [],
        "recoveryEstimate": {"actionsMinutes": "unknown", "r2StorageBytes": "unknown", "r2ClassA": "unknown", "r2ClassB": "unknown"},
        "resourceUsage": {
            "schemaVersion": 1,
            "metricsMeasuredAt": captured_at,
            "actual": {
                "d1RowsRead": usage["d1RowsRead"],
                "d1RowsWritten": usage["d1RowsWritten"],
                "workerRequests": usage["workerRequests"],
                "r2ClassA": usage["r2ClassA"],
                "r2ClassB": usage["r2ClassB"],
                "inventoryPages": len(pages),
                "responseBytes": sum(
                    page.response_bytes
                    for page in (transport_pages if transport_pages is not None else pages)
                ),
                "actionsMinutes": "unknown",
            },
            "estimated": {"d1RowsRead": "unknown", "d1RowsWritten": "unknown", "workerRequests": usage["workerRequests"], "r2ClassA": 0, "r2ClassB": usage["r2HeadCount"], "actionsMinutes": "unknown"},
        },
        "fingerprints": {
            "sourcePagesSha256": canonical_sha256(source_receipts),
            "inventorySha256": canonical_sha256(inventory),
            "r2IndexSha256": canonical_sha256(r2_index_payload),
        },
    }
    secret_path = _contains_secret_key(bundle)
    if secret_path:
        raise RecoveryEvidenceError("secret_field_rejected", secret_path)
    return bundle


def validate_evidence_bundle(bundle: Mapping[str, Any]) -> None:
    if bundle.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION or bundle.get("contract") != EVIDENCE_CONTRACT:
        raise RecoveryEvidenceError("evidence_contract_mismatch")
    scope = bundle.get("scope")
    if not isinstance(scope, Mapping):
        raise RecoveryEvidenceError("evidence_scope_missing")
    _require_scope(str(scope.get("startDate")), str(scope.get("endDateInclusive")), scope.get("priorityDates") or [])
    for field in ("inventory", "releaseManifests", "pipelineWindows", "r2Objects", "pages", "activeReleaseSlots", "errors"):
        if not isinstance(bundle.get(field), list):
            raise RecoveryEvidenceError(f"evidence_{field}_invalid")
    if len(bundle["pages"]) > MAX_PAGES or len(bundle["r2Objects"]) > MAX_R2_OBJECTS or len(bundle["activeReleaseSlots"]) > 2:
        raise RecoveryEvidenceError("evidence_bound_exceeded")
    if bundle.get("exportStatus") == "COMPLETE" and (not bundle["pages"] or bundle["errors"]):
        raise RecoveryEvidenceError("complete_export_without_complete_pages")
    secret_path = _contains_secret_key(bundle)
    if secret_path:
        raise RecoveryEvidenceError("secret_field_rejected", secret_path)
    dates = [item.get("dataDate") for item in bundle["inventory"] if isinstance(item, Mapping)]
    if any(not _valid_date(item) or item < SCOPE_START or item > SCOPE_END for item in dates):
        raise RecoveryEvidenceError("evidence_date_outside_scope")
    release_dates = [item.get("asOf") for item in bundle["releaseManifests"] if isinstance(item, Mapping)]
    pipeline_dates = [
        item.get(field)
        for item in bundle["pipelineWindows"] if isinstance(item, Mapping)
        for field in ("windowDate", "targetDate")
        if item.get(field) is not None
    ]
    r2_dates = [item.get("dataDate") for item in bundle["r2Objects"] if isinstance(item, Mapping) and item.get("dataDate") is not None]
    if any(not _valid_date(item) or item < SCOPE_START or item > SCOPE_END for item in (*release_dates, *pipeline_dates, *r2_dates)):
        raise RecoveryEvidenceError("evidence_date_outside_scope")


def to_audit_input(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Convert v2 evidence to the existing offline auditor's v1 contract."""

    validate_evidence_bundle(bundle)
    output = copy.deepcopy(dict(bundle))
    output.pop("contract", None)
    output["schemaVersion"] = 1
    output["domains"] = [
        {"id": domain, "market": market, "required": True}
        for domain, market in (
            ("sse_etf", "sse"), ("szse_etf", "szse"), ("sse_margin", "sse"),
            ("szse_margin", "szse"), ("bse_margin", "bse"), ("hkex_connect", None),
        )
    ]
    # Page bodies, fingerprints and exporter errors remain v2-only.  Account
    # provenance and task-local counters are retained so R0-D can distinguish
    # an optional account snapshot from its mandatory bounded read-only run.
    for field in ("fingerprints", "pages", "exportStatus", "errors"):
        output.pop(field, None)
    return output


def audit_twice(bundle: Mapping[str, Any], budget_config: Mapping[str, Any], *, phase: str = "R0-D") -> tuple[dict[str, Any], str]:
    audit_input = to_audit_input(bundle)
    first = audit_bundle(audit_input, budget_config=budget_config, phase=phase)
    second = audit_bundle(audit_input, budget_config=budget_config, phase=phase)
    first_hash = first["plan"]["planHash"]
    second_hash = second["plan"]["planHash"]
    if first_hash != second_hash:
        raise RecoveryEvidenceError("plan_hash_not_stable")
    return first, second_hash


def write_evidence_outputs(bundle: Mapping[str, Any], audit_result: Mapping[str, Any], output_dir: str | Path) -> None:
    output = Path(output_dir).resolve()
    assert_recovery_output_path(str(output / "_evidence_output_guard"))
    validate_evidence_bundle(bundle)
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence-bundle-v2.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_audit_outputs(audit_result, output)
    aliases = {
        "inventory-matrix.json": {
            "schemaVersion": 2,
            "scope": bundle["scope"],
            "planHash": audit_result["plan"]["planHash"],
            "rows": audit_result["inventory"],
            "dates": audit_result["dateSummaries"],
        },
        "source-unavailable.json": audit_result["sourceUnavailable"],
        "budget-report.json": audit_result["budget"],
        "r2-inventory.json": {"inventory": bundle["r2Inventory"], "objects": bundle["r2Objects"]},
        "resource-usage.json": bundle["resourceUsage"],
    }
    for name, value in aliases.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export bounded controlled-recovery evidence (read-only).")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--start", default=SCOPE_START)
    parser.add_argument("--end", default=SCOPE_END)
    parser.add_argument("--priority-date", action="append", dest="priority_dates")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--usage-file")
    parser.add_argument("--config", default="config/resource-budget.json")
    parser.add_argument("--phase", choices=("R0-C", "R0-D"), default="R0-D")
    parser.add_argument("--timeout", type=float, default=25.0)
    return parser


def _write_cli_error(output_dir: str | Path, error: RecoveryEvidenceError) -> None:
    output = Path(output_dir).resolve()
    assert_recovery_output_path(str(output / "_cli_error_guard"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "export-error.json").write_text(
        json.dumps({"status": "INCOMPLETE", "error": error.code}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_cli_audit_error(
    output_dir: str | Path,
    error: BaseException,
    *,
    bundle: Mapping[str, Any] | None = None,
) -> None:
    """Write bounded, actionable evidence when the offline audit rejects a bundle.

    The previous path recorded only ``RecoveryAuditError`` and discarded the
    error code and the otherwise valid v2 evidence bundle.  That made a
    read-only inventory failure impossible to diagnose from the workflow
    artifact.  Preserve the deterministic code/detail and the validated
    bundle, while never writing an unvalidated payload to the artifact.
    """

    output = Path(output_dir).resolve()
    assert_recovery_output_path(str(output / "_cli_audit_error_guard"))
    output.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": "INCOMPLETE",
        "error": getattr(error, "code", type(error).__name__),
        "detail": str(error),
    }
    if isinstance(error, RecoveryAuditError):
        payload["errorType"] = "RecoveryAuditError"
    if bundle is not None:
        try:
            validate_evidence_bundle(bundle)
        except (RecoveryEvidenceError, ValueError, OSError, json.JSONDecodeError):
            pass
        else:
            (output / "evidence-bundle-v2.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            payload["bundleStatus"] = bundle.get("exportStatus")
            errors = bundle.get("errors")
            payload["bundleErrorCount"] = len(errors) if isinstance(errors, list) else 0
    (output / "export-error.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    priority_values = args.priority_dates or list(PRIORITY_DATES)
    priority = [item.strip() for value in priority_values for item in str(value).split(",") if item.strip()]
    assert_recovery_output_path(str(Path(args.output_dir).resolve() / "_cli_output_guard"))
    captured_at = _utc_now()
    errors: list[dict[str, Any]] = []
    usage, usage_status = load_official_usage(args.usage_file)
    try:
        exporter = RecoveryEvidenceExporter(
            base_url=args.base_url,
            start=args.start,
            end=args.end,
            priority_dates=priority,
            ingest_api_key=os.environ.get("FLOW_INGEST_API_KEY"),
            access_client_id=os.environ.get("FLOW_CF_ACCESS_CLIENT_ID"),
            access_client_secret=os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET"),
            timeout=args.timeout,
            captured_at=captured_at,
        )
        result = exporter.collect()
        bundle = result.bundle
        bundle["accountTelemetry"] = usage
        bundle["accountTelemetryEvidence"] = usage_status
        errors.extend(result.errors)
    except RecoveryEvidenceError as error:
        item: dict[str, Any] = {"code": error.code, "detail": str(error)}
        if error.evidence is not None:
            item["evidence"] = copy.deepcopy(error.evidence)
        errors.append(item)
        if args.start != SCOPE_START or args.end != SCOPE_END or any(
            not _valid_date(item) or item < SCOPE_START or item > SCOPE_END for item in priority
        ):
            _write_cli_error(args.output_dir, error)
            return 2
        bundle = build_evidence_bundle(
            pages=[], start=args.start, end=args.end, priority_dates=priority,
            captured_at=captured_at, errors=errors, official_usage=usage, usage_status=usage_status,
        )
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    try:
        audit_result, second_hash = audit_twice(bundle, config, phase=args.phase)
        bundle["planHashReplay"] = second_hash
        write_evidence_outputs(bundle, audit_result, args.output_dir)
    except (RecoveryEvidenceError, RecoveryAuditError, ValueError, OSError, json.JSONDecodeError) as error:
        _write_cli_audit_error(args.output_dir, error, bundle=bundle)
        return 2
    print(json.dumps({"status": bundle["exportStatus"], "pages": len(bundle["pages"]), "planHash": second_hash, "outputDir": str(Path(args.output_dir).resolve())}, ensure_ascii=False))
    return 0 if bundle["exportStatus"] == "COMPLETE" and not errors else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
