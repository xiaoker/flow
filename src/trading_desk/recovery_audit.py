"""Deterministic, bounded and read-only controlled-recovery inventory.

The R0-C auditor consumes a *local evidence bundle* exported by a bounded
read-only collector.  It never opens a network connection, invokes a provider
CLI, writes D1/R2, dispatches a workflow, changes a flag, or mutates an active
release.  Keeping the boundary explicit is intentional: a later R0-D runner
may supply fresh evidence, but it must not turn the inventory command into a
hidden recovery executor.

Input bundle (schemaVersion=1) has these optional/required top-level fields::

    {
      "schemaVersion": 1,
      "campaignId": "r0-20260905",
      "scope": {
        "startDate": "2024-08-01",
        "endDateInclusive": "2026-09-03",
        "priorityDates": ["2026-08-28"]
      },
      "domains": [{"id": "sse_etf", "market": "sse", "required": true}],
      "inventory": [{"dataDate": "2026-08-28", "domain": "sse_etf", ...}],
      "releaseManifests": [{"asOf": "2026-08-28", "slot": "dashboard",
                             "complete": true}],
      "pipelineWindows": [],
      "r2Objects": [],
      "accountTelemetry": {},
      "d1Audit": {}
    }

An inventory row may use the legacy flat fields (``rawStatus``,
``normalizedStatus``, ``modelStatus``, ``publicationStatus`` and ``slots``) or
the canonical ``stageStatus`` map.  Missing evidence is retained as UNKNOWN;
it is never inferred to be a successful stage.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .recovery_execution import (
    RecoveryContractError,
    assert_recovery_output_path,
    canonical_sha256,
    parse_run_key,
)


INPUT_SCHEMA_VERSION = 1
AUDIT_SCHEMA_VERSION = 2
# ``AUDIT_SCHEMA_VERSION`` is retained for the plan and budget payloads,
# which already use the repository-wide numeric v2 convention.  Stage
# evidence has its own explicit contract identifier so a consumer cannot
# confuse the R0-D state machine with an unrelated schema that also happens
# to be version 2.
STAGE_EVIDENCE_SCHEMA_VERSION = "recovery-stage-evidence-v2"
AUDIT_CONTRACT_VERSION = "recovery-audit-v2"
AUDIT_PHASES = frozenset({"R0-C", "R0-D"})
RECONCILIATION_STATUSES = frozenset({"NOT_APPLICABLE", "PENDING", "R0_RECONCILED"})
NEXT_ALLOWED_ACTIONS = frozenset({"STOP", "R0-D_RECONCILE", "R2_ACCEPTANCE_AUDIT"})
MAX_CALENDAR_DAYS = 1_100
MAX_INPUT_RECORDS = 10_000
MAX_RELEASE_RECORDS = 5_000
MAX_PIPELINE_RECORDS = 500
MAX_R2_RECORDS = 20_000
MAX_R2_PAGE_SIZE = MAX_R2_RECORDS
MAX_R2_GAP_SAMPLES = 500
MAX_R2_GAP_COUNT = MAX_R2_RECORDS
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# These identifiers mirror SOURCE_DEFINITIONS in source_readiness.py.  The
# auditor deliberately owns no second business taxonomy; it only gives the
# existing IDs a stable order and market label for the matrix.
DEFAULT_DOMAINS: tuple[dict[str, Any], ...] = (
    {"id": "sse_etf", "market": "sse", "required": True},
    {"id": "szse_etf", "market": "szse", "required": True},
    {"id": "sse_margin", "market": "sse", "required": True},
    {"id": "szse_margin", "market": "szse", "required": True},
    {"id": "bse_margin", "market": "bse", "required": True},
    {"id": "hkex_connect", "market": None, "required": True},
)
AUDIT_STAGES: tuple[str, ...] = (
    "raw",
    "normalized",
    "model",
    "pcf_exact",
    "dashboard",
    "etf_penetration",
    "date_visible",
)
RECOVERY_STAGE_MAP = {
    "raw": "core_collection",
    "normalized": "core_collection",
    "model": "stock_flow",
    "pcf_exact": "pcf_exact",
    "dashboard": "dashboard",
    "etf_penetration": "etf_penetration",
    "date_visible": "publish",
}
READY_STATES = frozenset({"READY", "COMPLETE", "COMPLETED", "PASS", "CONFIRMED", "ACTIVE"})
KNOWN_STATES = frozenset(
    {
        "READY",
        "COMPLETE",
        "COMPLETED",
        "PASS",
        "CONFIRMED",
        "ACTIVE",
        "MISSING",
        "UNKNOWN",
        "INVALID",
        "BLOCKED",
        "BLOCKED_QUALITY",
        "SOURCE_UNAVAILABLE",
        "OFFICIAL_CLOSED",
        "NOT_APPLICABLE",
        "PENDING",
    }
)
TELEMETRY_FIELDS = (
    "d1RowsReadPerDay",
    "d1RowsWrittenPerDay",
    "workersRequestsPerDay",
    "githubActionsMinutes",
    "r2StorageBytes",
    "r2ClassA",
    "r2ClassB",
)
READ_ONLY_INVENTORY_USAGE_FIELDS = (
    "d1RowsRead",
    "d1RowsWritten",
    "workerRequests",
    "r2ClassA",
    "r2ClassB",
    "inventoryPages",
    "responseBytes",
)
SECRET_KEY_PARTS = ("token", "secret", "password", "cookie", "authorization", "access_key")


class RecoveryAuditError(ValueError):
    """A deterministic input, bound or evidence contract failure."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _reject_secret_keys(value: Any, path: str = "bundle") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SECRET_KEY_PARTS):
                raise RecoveryAuditError("secret_field_rejected", f"{path}.{key}")
            _reject_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}[{index}]")


def _text(value: Any, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryAuditError(f"invalid_{field}")
    result = value.strip()
    if len(result) > max_length:
        raise RecoveryAuditError(f"{field}_too_long")
    return result


def _optional_text(value: Any, field: str, *, max_length: int = 512) -> str | None:
    if value in (None, ""):
        return None
    return _text(value, field, max_length=max_length)


def _date_text(value: Any, field: str) -> str:
    result = _text(value, field, max_length=10)
    if not ISO_DATE_RE.fullmatch(result):
        raise RecoveryAuditError(f"invalid_{field}")
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise RecoveryAuditError(f"invalid_{field}") from exc
    return result


def _optional_date(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _date_text(value, field)


def _valid_instant(value: Any) -> bool:
    """Return whether a provenance timestamp is an ISO-8601 instant."""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _nonnegative_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryAuditError(f"invalid_{field}")
    if maximum is not None and value > maximum:
        raise RecoveryAuditError(f"{field}_exceeds_limit")
    return value


def _status(value: Any, field: str) -> str:
    if value in (None, ""):
        return "UNKNOWN"
    result = _text(value, field, max_length=64).upper().replace("-", "_")
    if result not in KNOWN_STATES:
        # Preserve unknown provider states as UNKNOWN instead of treating a
        # novel status as success.  The original value is retained separately
        # in statusNotes when supplied by the caller.
        return "UNKNOWN"
    return result


def _is_ready(value: str) -> bool:
    return value in READY_STATES


def _list_of_text(value: Any, field: str, *, max_count: int = 500) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > max_count:
        raise RecoveryAuditError(f"invalid_{field}")
    values = sorted({_text(item, f"{field}_item", max_length=512) for item in value})
    return values


def _sha(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    result = _text(value, field, max_length=64).lower()
    if not SHA256_RE.fullmatch(result):
        raise RecoveryAuditError(f"invalid_{field}")
    return result


def _scope(bundle: Mapping[str, Any]) -> tuple[str, str, list[str], list[str]]:
    raw_scope = bundle.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise RecoveryAuditError("scope_required")
    start = _date_text(raw_scope.get("startDate"), "scope_start_date")
    end = _date_text(raw_scope.get("endDateInclusive"), "scope_end_date")
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    if end_day < start_day:
        raise RecoveryAuditError("scope_date_order_invalid")
    days = (end_day - start_day).days + 1
    if days > MAX_CALENDAR_DAYS:
        raise RecoveryAuditError("scope_exceeds_limit")
    priority_raw = raw_scope.get("priorityDates") or []
    if not isinstance(priority_raw, list):
        raise RecoveryAuditError("invalid_priority_dates")
    priority = sorted({_date_text(item, "priority_date") for item in priority_raw})
    if any(item < start or item > end for item in priority):
        raise RecoveryAuditError("priority_date_outside_scope")
    dates = [
        (start_day + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]
    return start, end, priority, dates


def _domains(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = bundle.get("domains")
    values = list(DEFAULT_DOMAINS) if raw is None else raw
    if not isinstance(values, list) or not values:
        raise RecoveryAuditError("domains_required")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            raise RecoveryAuditError("invalid_domain")
        domain_id = _text(item.get("id"), "domain_id", max_length=80)
        if domain_id in seen:
            raise RecoveryAuditError("duplicate_domain")
        seen.add(domain_id)
        market = _optional_text(item.get("market"), "domain_market", max_length=32)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise RecoveryAuditError("invalid_domain_required")
        result.append({"id": domain_id, "market": market, "required": required})
    return result


def _release_index(bundle: Mapping[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    raw = bundle.get("releaseManifests") or []
    if not isinstance(raw, list) or len(raw) > MAX_RELEASE_RECORDS:
        raise RecoveryAuditError("release_manifest_exceeds_limit")
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in raw:
        if not isinstance(item, Mapping):
            raise RecoveryAuditError("invalid_release_manifest")
        as_of = _date_text(item.get("asOf", item.get("as_of")), "release_as_of")
        slot = _text(item.get("slot"), "release_slot", max_length=64)
        complete = item.get("complete")
        if not isinstance(complete, bool):
            raise RecoveryAuditError("release_complete_required")
        result[(as_of, slot)].append(
            {
                "releaseId": _optional_text(item.get("releaseId", item.get("release_id")), "release_id"),
                "asOf": as_of,
                "slot": slot,
                "complete": complete,
                "status": _optional_text(item.get("status"), "release_status", max_length=64),
                "manifestSha256": _sha(item.get("manifestSha256", item.get("manifest_sha256")), "manifest_sha256"),
            }
        )
    return result


def _slot_status(
    *,
    data_date: str,
    slot: str,
    row: Mapping[str, Any],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> str:
    stage_status = row.get("stageStatus")
    if isinstance(stage_status, Mapping) and slot in stage_status:
        return _status(stage_status.get(slot), f"stage_status_{slot}")
    slot_statuses = row.get("slotStatuses")
    if isinstance(slot_statuses, Mapping) and slot in slot_statuses:
        return _status(slot_statuses.get(slot), f"slot_status_{slot}")
    records = releases.get((data_date, slot), [])
    if records:
        complete_values = {record["complete"] for record in records}
        if complete_values == {True, False}:
            return "BLOCKED"
        return "READY" if True in complete_values else "MISSING"
    slots = set(_list_of_text(row.get("slots"), "slots", max_count=64))
    if slot in slots:
        return "READY"
    evidence = row.get("evidence")
    if isinstance(evidence, Mapping):
        evidence_value = evidence.get("dashboardRelease" if slot == "dashboard" else "penetrationRelease")
        if isinstance(evidence_value, Mapping):
            if evidence_value.get("complete") is True:
                return "READY"
            if evidence_value.get("complete") is False:
                return "MISSING"
    return "UNKNOWN"


def _row_stage_status(
    row: Mapping[str, Any],
    stage: str,
    *,
    data_date: str,
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> str:
    stage_status = row.get("stageStatus")
    if isinstance(stage_status, Mapping) and stage in stage_status:
        return _status(stage_status.get(stage), f"stage_status_{stage}")
    if stage == "raw":
        return _status(row.get("rawStatus"), "raw_status")
    if stage == "normalized":
        return _status(row.get("normalizedStatus"), "normalized_status")
    if stage == "model":
        return _status(row.get("modelStatus"), "model_status")
    if stage == "pcf_exact":
        return _status(row.get("pcfStatus", row.get("pcfExactStatus")), "pcf_status")
    if stage in {"dashboard", "etf_penetration"}:
        return _slot_status(data_date=data_date, slot=stage, row=row, releases=releases)
    if stage == "date_visible":
        explicit = row.get("dateVisibleStatus", row.get("date_visible"))
        if explicit not in (None, ""):
            return _status(explicit, "date_visible_status")
        publication = _status(row.get("publicationStatus"), "publication_status")
        if publication != "UNKNOWN":
            return publication
        dashboard = _slot_status(data_date=data_date, slot="dashboard", row=row, releases=releases)
        return "READY" if dashboard == "READY" else dashboard
    raise RecoveryAuditError("unknown_audit_stage", stage)


def _applicability(row: Mapping[str, Any], domain: Mapping[str, Any]) -> str:
    value = row.get("applicability")
    if value in (None, ""):
        # Omitted applicability is deliberately UNKNOWN, even for a required
        # domain.  The source calendar must explicitly establish a required or
        # official-closed date before recovery can be planned.
        return "UNKNOWN"
    result = _text(value, "applicability", max_length=32).upper().replace("-", "_")
    if result not in {"REQUIRED", "OFFICIAL_CLOSED", "NOT_APPLICABLE", "UNKNOWN"}:
        raise RecoveryAuditError("invalid_applicability")
    return result


def _normalise_row(
    row: Mapping[str, Any],
    *,
    domain: Mapping[str, Any],
    data_date: str,
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    source_id_key_present = "sourceId" in row or "source_id" in row
    source_id_value = row.get("sourceId", row.get("source_id"))
    raw_sha = _sha(
        row.get("rawSha256", row.get("raw_sha256", row.get("declaredRawSha256", row.get("declared_raw_sha256")))),
        "raw_sha256",
    )
    normalized_sha = _sha(
        row.get(
            "normalizedSha256",
            row.get("normalized_sha256", row.get("declaredNormalizedSha256", row.get("declared_normalized_sha256"))),
        ),
        "normalized_sha256",
    )
    computed_raw_sha = _sha(
        row.get("computedRawSha256", row.get("computed_raw_sha256", row.get("archivedBytesSha256"))),
        "computed_raw_sha256",
    )
    computed_normalized_sha = _sha(
        row.get("computedNormalizedSha256", row.get("computed_normalized_sha256")),
        "computed_normalized_sha256",
    )
    declared_raw_sha = _sha(
        row.get("declaredRawSha256", row.get("declared_raw_sha256")),
        "declared_raw_sha256",
    )
    declared_normalized_sha = _sha(
        row.get("declaredNormalizedSha256", row.get("declared_normalized_sha256")),
        "declared_normalized_sha256",
    )
    hash_status = _status(row.get("hashStatus", row.get("hash_status")), "hash_status")
    hash_mismatch = row.get("hashMismatch") is True
    if declared_raw_sha and computed_raw_sha and declared_raw_sha != computed_raw_sha:
        hash_mismatch = True
    if declared_normalized_sha and computed_normalized_sha and declared_normalized_sha != computed_normalized_sha:
        hash_mismatch = True
    if hash_mismatch:
        hash_status = "BLOCKED"
    elif hash_status == "UNKNOWN" and (raw_sha or normalized_sha):
        # A caller may explicitly say READY, or may provide a computed hash
        # for an object whose old HTTP header was absent.  In the latter case
        # the provenance below remains visible and the bytes are still
        # usable; only a declared/computed mismatch is a hard block.
        hash_status = "READY" if computed_raw_sha or computed_normalized_sha else hash_status
    stages = {
        stage: _row_stage_status(row, stage, data_date=data_date, releases=releases)
        for stage in AUDIT_STAGES
    }
    # A stage that claims READY without a corresponding digest is not
    # considered complete evidence.  This prevents an existence-only index
    # hit from becoming a recovery target or a false DONE result.
    if row.get("rawKey", row.get("raw_key")) and not raw_sha and stages["raw"] == "READY":
        stages["raw"] = "UNKNOWN"
    if row.get("normalizedKey", row.get("normalized_key")) and not normalized_sha and stages["normalized"] == "READY":
        stages["normalized"] = "UNKNOWN"
    if hash_mismatch:
        if declared_raw_sha or computed_raw_sha or row.get("hashMismatch") is True:
            stages["raw"] = "BLOCKED"
        if declared_normalized_sha or computed_normalized_sha:
            stages["normalized"] = "BLOCKED"
    result: dict[str, Any] = {
        "dataDate": data_date,
        "domain": domain["id"],
        "market": domain.get("market"),
        "required": bool(domain.get("required")),
        "applicability": _applicability(row, domain),
        "stages": stages,
        "rawKey": _optional_text(row.get("rawKey", row.get("raw_key")), "raw_key", max_length=1_024),
        "rawSha256": raw_sha,
        "declaredRawSha256": declared_raw_sha,
        "computedRawSha256": computed_raw_sha,
        "normalizedKey": _optional_text(row.get("normalizedKey", row.get("normalized_key")), "normalized_key", max_length=1_024),
        "normalizedSha256": normalized_sha,
        "declaredNormalizedSha256": declared_normalized_sha,
        "computedNormalizedSha256": computed_normalized_sha,
        "hashOrigin": _optional_text(row.get("hashOrigin", row.get("hash_origin")), "hash_origin", max_length=128),
        "sourceBusinessDate": _optional_date(row.get("sourceBusinessDate", row.get("source_business_date")), "source_business_date"),
        # Domain IDs are the repository's canonical source IDs.  A producer
        # may repeat the value explicitly, but the normalized row must remain
        # auditable for legacy evidence that only carries the domain key.
        "sourceId": (
            _optional_text(source_id_value, "source_id", max_length=120)
            if source_id_key_present
            else domain["id"]
        ),
        "evidenceSource": _optional_text(
            row.get("evidenceSource", row.get("evidence_source")),
            "evidence_source",
            max_length=256,
        ),
        "evidenceCapturedAt": _optional_text(
            row.get(
                "evidenceCapturedAt",
                row.get(
                    "evidence_captured_at",
                    row.get(
                        "checkedAt",
                        row.get(
                            "checked_at",
                            row.get("observedAt", row.get("collectedAt", row.get("collectionTime"))),
                        ),
                    ),
                ),
            ),
            "evidence_captured_at",
            max_length=64,
        ),
        "publicationDate": _optional_date(row.get("publicationDate", row.get("publication_date")), "publication_date"),
        "navDate": _optional_date(row.get("navDate", row.get("nav_date")), "nav_date"),
        "pcfDate": _optional_date(row.get("pcfDate", row.get("pcf_date")), "pcf_date"),
        "effectiveDate": _optional_date(row.get("effectiveDate", row.get("effective_date")), "effective_date") or data_date,
        "rows": _nonnegative_int(row.get("rows", 0), "rows", maximum=100_000_000),
        "unit": _optional_text(row.get("unit"), "unit", max_length=64),
        "schemaVersion": row.get("schemaVersion", row.get("schema_version")),
        "calculationVersion": _optional_text(row.get("calculationVersion", row.get("calculation_version")), "calculation_version", max_length=128),
        "sourceFingerprint": _sha(row.get("sourceFingerprint", row.get("source_fingerprint")), "source_fingerprint"),
        "runKeys": _list_of_text(row.get("runKeys", row.get("run_keys")), "run_keys"),
        "releaseIds": _list_of_text(row.get("releaseIds", row.get("release_ids")), "release_ids"),
        "slots": _list_of_text(row.get("slots"), "slots", max_count=64),
        "gapReason": _optional_text(row.get("gapReason", row.get("gap_reason")), "gap_reason", max_length=512),
        "nextStage": _optional_text(row.get("nextStage", row.get("next_stage")), "next_stage", max_length=128),
        "hashStatus": hash_status,
        "hashMismatch": hash_mismatch,
    }
    evidence = row.get("evidence")
    if isinstance(evidence, Mapping):
        result["evidence"] = {
            "marketHistory": bool(evidence.get("marketHistory")),
            "historicalSnapshot": bool(evidence.get("historicalSnapshot")),
            "dashboardRelease": bool(evidence.get("dashboardRelease")),
            "penetrationRelease": bool(evidence.get("penetrationRelease")),
            "sourceReadinessCount": len(evidence.get("sourceReadiness") or [])
            if isinstance(evidence.get("sourceReadiness"), list)
            else 0,
        }
        applicability_source = evidence.get("applicabilitySource")
        if isinstance(applicability_source, str) and applicability_source:
            result["evidence"]["applicabilitySource"] = applicability_source
        evidence_source_id = evidence.get("sourceId", evidence.get("source_id"))
        if isinstance(evidence_source_id, str) and evidence_source_id:
            result["sourceId"] = result["sourceId"] or evidence_source_id
        evidence_business_date = evidence.get(
            "sourceBusinessDate",
            evidence.get("source_business_date", evidence.get("businessDate")),
        )
        if result["sourceBusinessDate"] is None and evidence_business_date not in (None, ""):
            result["sourceBusinessDate"] = _date_text(evidence_business_date, "source_business_date")
        evidence_fingerprint = evidence.get("sourceFingerprint", evidence.get("source_fingerprint"))
        if result["sourceFingerprint"] is None and evidence_fingerprint not in (None, ""):
            result["sourceFingerprint"] = _sha(evidence_fingerprint, "source_fingerprint")
        evidence_reason = evidence.get("gapReason", evidence.get("reason"))
        if result["gapReason"] is None and isinstance(evidence_reason, str) and evidence_reason:
            result["gapReason"] = _text(evidence_reason, "gap_reason", max_length=512)
        evidence_source = evidence.get("evidenceSource", evidence.get("source"))
        if isinstance(evidence_source, str) and evidence_source:
            result["evidenceSource"] = result["evidenceSource"] or evidence_source
        source_evidence = evidence.get("sourceEvidence")
        if isinstance(source_evidence, str) and source_evidence:
            result["evidenceSource"] = result["evidenceSource"] or source_evidence
        if source_evidence is True and not result["evidenceSource"]:
            # The v2 exporter currently records a boolean source receipt.  It
            # is still a real evidence source, but its stable label must be
            # explicit in the normalized row rather than inferred from a UI
            # status.
            result["evidenceSource"] = "sourceEvidence"
        evidence_captured_at = evidence.get(
            "capturedAt",
            evidence.get(
                "captured_at",
                evidence.get(
                    "checkedAt",
                    evidence.get(
                        "checked_at",
                        evidence.get("observedAt", evidence.get("collectedAt", evidence.get("collectionTime"))),
                    ),
                ),
            ),
        )
        if isinstance(evidence_captured_at, str) and evidence_captured_at:
            result["evidenceCapturedAt"] = result["evidenceCapturedAt"] or evidence_captured_at
    else:
        result["evidence"] = {
            "marketHistory": False,
            "historicalSnapshot": False,
            "dashboardRelease": False,
            "penetrationRelease": False,
        "sourceReadinessCount": 0,
    }
    return result


def _default_row(domain: Mapping[str, Any], data_date: str) -> dict[str, Any]:
    return _normalise_row({}, domain=domain, data_date=data_date, releases={})


def _validate_inventory(
    bundle: Mapping[str, Any],
    *,
    dates: Sequence[str],
    domains: Sequence[Mapping[str, Any]],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    raw = bundle.get("inventory") or []
    if not isinstance(raw, list) or len(raw) > MAX_INPUT_RECORDS:
        raise RecoveryAuditError("inventory_exceeds_limit")
    domain_map = {item["id"]: item for item in domains}
    date_set = set(dates)
    values: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise RecoveryAuditError("invalid_inventory_row")
        data_date = _date_text(item.get("dataDate", item.get("date")), "inventory_data_date")
        if data_date not in date_set:
            raise RecoveryAuditError("inventory_date_outside_scope")
        domain_id = _text(item.get("domain"), "inventory_domain", max_length=80)
        domain = domain_map.get(domain_id)
        if domain is None:
            raise RecoveryAuditError("inventory_domain_unknown", domain_id)
        key = (data_date, domain_id)
        if key in values:
            raise RecoveryAuditError("duplicate_inventory_row", f"{data_date}:{domain_id}")
        normalized = _normalise_row(
            item,
            domain=domain,
            data_date=data_date,
            releases=releases,
        )
        # The formal evidence exporter records one immutable capture time for
        # the bundle.  Use it as the receipt time when a legacy row omitted a
        # nested timestamp; this keeps the source-unavailable provenance
        # complete without inventing a time or using the UI date.
        captured_at = bundle.get("capturedAt", bundle.get("captured_at"))
        if normalized["evidenceCapturedAt"] is None and isinstance(captured_at, str) and captured_at.strip():
            normalized["evidenceCapturedAt"] = captured_at.strip()
        values[key] = normalized
    output: list[dict[str, Any]] = []
    for data_date in dates:
        for domain in domains:
            output.append(values.get((data_date, domain["id"])) or _default_row(domain, data_date))
    return output


def _global_slot_status(
    data_date: str,
    slot: str,
    *,
    rows: Sequence[Mapping[str, Any]],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> str:
    release_records = releases.get((data_date, slot), [])
    if release_records:
        complete_values = {record["complete"] for record in release_records}
        if complete_values == {True, False}:
            return "BLOCKED"
        return "READY" if True in complete_values else "MISSING"
    date_rows = [row for row in rows if row.get("dataDate") == data_date]
    applicable_rows = [
        row
        for row in date_rows
        if row.get("required") and row.get("applicability") == "REQUIRED"
    ]
    if not applicable_rows and date_rows and all(
        row.get("applicability") in {"OFFICIAL_CLOSED", "NOT_APPLICABLE"}
        for row in date_rows
    ):
        return "NOT_APPLICABLE"
    statuses = [str(row.get("stages", {}).get(slot, "UNKNOWN")) for row in applicable_rows]
    if not statuses:
        return "UNKNOWN"
    if any(status in {"BLOCKED", "BLOCKED_QUALITY", "INVALID"} for status in statuses):
        return "BLOCKED"
    if any(status == "UNKNOWN" or status == "PENDING" for status in statuses):
        return "UNKNOWN"
    if any(status == "MISSING" for status in statuses):
        return "MISSING"
    return "READY" if all(_is_ready(status) for status in statuses) else "UNKNOWN"


def _date_visible_status(
    data_date: str,
    *,
    rows: Sequence[Mapping[str, Any]],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> str:
    date_rows = [row for row in rows if row.get("dataDate") == data_date]
    applicable_rows = [
        row
        for row in date_rows
        if row.get("required") and row.get("applicability") == "REQUIRED"
    ]
    if not applicable_rows and date_rows and all(
        row.get("applicability") in {"OFFICIAL_CLOSED", "NOT_APPLICABLE"}
        for row in date_rows
    ):
        return "NOT_APPLICABLE"
    statuses = [str(row.get("stages", {}).get("date_visible", "UNKNOWN")) for row in applicable_rows]
    if any(status in {"BLOCKED", "BLOCKED_QUALITY", "INVALID"} for status in statuses):
        return "BLOCKED"
    if any(status == "UNKNOWN" or status == "PENDING" for status in statuses):
        return "UNKNOWN"
    if statuses and all(_is_ready(status) for status in statuses):
        return "READY"
    dashboard = _global_slot_status(data_date, "dashboard", rows=rows, releases=releases)
    if dashboard == "READY":
        return "READY"
    if "MISSING" in statuses or dashboard == "MISSING":
        return "MISSING"
    return "UNKNOWN"


def _aggregate_stage(rows: Sequence[Mapping[str, Any]], stage: str) -> str:
    statuses = [str(row.get("stages", {}).get(stage, "UNKNOWN")) for row in rows]
    if not statuses:
        return "UNKNOWN"
    if any(status in {"BLOCKED", "BLOCKED_QUALITY", "INVALID"} for status in statuses):
        return "BLOCKED"
    if any(status == "SOURCE_UNAVAILABLE" for status in statuses):
        return "SOURCE_UNAVAILABLE"
    if any(status in {"UNKNOWN", "PENDING"} for status in statuses):
        return "UNKNOWN"
    if any(status in {"MISSING", "OFFICIAL_CLOSED", "NOT_APPLICABLE"} for status in statuses):
        return "MISSING"
    return "READY" if all(_is_ready(status) for status in statuses) else "UNKNOWN"


def _row_hash_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dataDate": row["dataDate"],
        "domain": row["domain"],
        "market": row["market"],
        "required": row["required"],
        "applicability": row["applicability"],
        "stages": row["stages"],
        "sourceId": row["sourceId"],
        "evidenceSource": row["evidenceSource"],
        "rawKey": row["rawKey"],
        "rawSha256": row["rawSha256"],
        "declaredRawSha256": row["declaredRawSha256"],
        "computedRawSha256": row["computedRawSha256"],
        "normalizedKey": row["normalizedKey"],
        "normalizedSha256": row["normalizedSha256"],
        "declaredNormalizedSha256": row["declaredNormalizedSha256"],
        "computedNormalizedSha256": row["computedNormalizedSha256"],
        "hashOrigin": row["hashOrigin"],
        "sourceBusinessDate": row["sourceBusinessDate"],
        "publicationDate": row["publicationDate"],
        "navDate": row["navDate"],
        "pcfDate": row["pcfDate"],
        "effectiveDate": row["effectiveDate"],
        "rows": row["rows"],
        "unit": row["unit"],
        "schemaVersion": row["schemaVersion"],
        "calculationVersion": row["calculationVersion"],
        "sourceFingerprint": row["sourceFingerprint"],
        "runKeys": row["runKeys"],
        "releaseIds": row["releaseIds"],
        "slots": row["slots"],
        "hashStatus": row["hashStatus"],
        "hashMismatch": row["hashMismatch"],
        "evidence": row["evidence"],
    }


def _validate_pipeline_windows(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = bundle.get("pipelineWindows") or []
    if not isinstance(raw, list) or len(raw) > MAX_PIPELINE_RECORDS:
        raise RecoveryAuditError("pipeline_windows_exceeds_limit")
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise RecoveryAuditError("invalid_pipeline_window")
        run_key = _text(item.get("runKey", item.get("run_key")), "run_key", max_length=220)
        try:
            parsed_run_key = parse_run_key(run_key)
        except RecoveryContractError as exc:
            raise RecoveryAuditError("invalid_run_key", run_key) from exc
        if run_key in seen:
            raise RecoveryAuditError("duplicate_run_key", run_key)
        seen.add(run_key)
        window_date = _optional_date(item.get("windowDate", item.get("window_date")), "window_date")
        if window_date and window_date != parsed_run_key["date"]:
            raise RecoveryAuditError("run_key_window_date_mismatch", run_key)
        output.append(
            {
                "runKey": run_key,
                "pipeline": _optional_text(item.get("pipeline"), "pipeline", max_length=80),
                "windowDate": window_date,
                "windowName": _optional_text(item.get("windowName", item.get("window_name")), "window_name", max_length=64),
                "targetDate": _optional_date(item.get("targetDate", item.get("target_date")), "target_date"),
                "status": _status(item.get("status"), "pipeline_status"),
                "attemptCount": _nonnegative_int(item.get("attemptCount", item.get("attempt_count", 0)), "attempt_count", maximum=10_000),
                "lastErrorCode": _optional_text(item.get("lastErrorCode", item.get("last_error_code")), "last_error_code", max_length=128),
            }
        )
    return sorted(output, key=lambda item: item["runKey"])


def _validate_r2_objects(bundle: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    raw = bundle.get("r2Objects")
    if raw is None:
        return [], "UNKNOWN"
    if not isinstance(raw, list) or len(raw) > MAX_R2_RECORDS:
        raise RecoveryAuditError("r2_objects_exceeds_limit")
    # A bounded object page is useful only when the evidence says how it was
    # obtained.  We intentionally do not accept a naked list as proof that a
    # bucket was exhaustively inspected: the caller must provide the manifest
    # or index page, its hard cap and whether more pages remain.
    page = bundle.get("r2Inventory")
    if not isinstance(page, Mapping):
        return [], "UNKNOWN"
    page_number = _nonnegative_int(page.get("page", 0), "r2_page", maximum=100_000)
    page_size = _nonnegative_int(page.get("pageSize", len(raw)), "r2_page_size", maximum=MAX_R2_PAGE_SIZE)
    hard_limit = _nonnegative_int(page.get("hardLimit"), "r2_hard_limit", maximum=MAX_R2_RECORDS)
    if page_size > hard_limit:
        raise RecoveryAuditError("r2_page_exceeds_hard_limit")
    if len(raw) > page_size:
        raise RecoveryAuditError("r2_page_count_exceeds_page_size")
    if not isinstance(page.get("hasMore"), bool):
        raise RecoveryAuditError("r2_has_more_required")
    suspicious_gaps = page.get("suspiciousGaps", page.get("suspicious_gaps", []))
    if not isinstance(suspicious_gaps, list) or len(suspicious_gaps) > MAX_R2_GAP_SAMPLES:
        raise RecoveryAuditError("invalid_r2_suspicious_gaps")
    suspicious_gaps = _list_of_text(suspicious_gaps, "r2_suspicious_gaps", max_count=MAX_R2_GAP_SAMPLES)
    suspicious_gap_count = _nonnegative_int(
        page.get("suspiciousGapCount", page.get("suspicious_gap_count", len(suspicious_gaps))),
        "r2_suspicious_gap_count",
        maximum=MAX_R2_GAP_COUNT,
    )
    if suspicious_gap_count < len(suspicious_gaps):
        raise RecoveryAuditError("r2_suspicious_gap_count_below_sample")
    suspicious_gaps_truncated = page.get(
        "suspiciousGapsTruncated",
        page.get("suspicious_gaps_truncated", suspicious_gap_count > len(suspicious_gaps)),
    )
    if not isinstance(suspicious_gaps_truncated, bool) or suspicious_gaps_truncated != (suspicious_gap_count > len(suspicious_gaps)):
        raise RecoveryAuditError("invalid_r2_suspicious_gap_truncation")
    suspicious_gaps_sha256 = _sha(
        page.get("suspiciousGapsSha256", page.get("suspicious_gaps_sha256")),
        "r2_suspicious_gaps_sha256",
    )
    if suspicious_gaps_truncated and not suspicious_gaps_sha256:
        raise RecoveryAuditError("r2_suspicious_gaps_hash_required")
    if suspicious_gaps_sha256 and not suspicious_gaps_truncated and suspicious_gaps_sha256 != canonical_sha256(suspicious_gaps):
        raise RecoveryAuditError("r2_suspicious_gaps_hash_mismatch")
    index_sha = _sha(page.get("indexSha256", page.get("index_sha256")), "r2_index_sha256")
    if not index_sha:
        raise RecoveryAuditError("r2_index_sha_required")
    computed_index_sha = _sha(
        page.get("computedIndexSha256", page.get("computed_index_sha256")),
        "r2_computed_index_sha256",
    )
    if computed_index_sha and computed_index_sha != index_sha:
        raise RecoveryAuditError("r2_index_sha256_mismatch")
    next_cursor = page.get("nextCursor", page.get("next_cursor"))
    if next_cursor not in (None, ""):
        _text(next_cursor, "r2_next_cursor", max_length=256)
    # A page other than the first cannot prove that the preceding pages were
    # observed.  The formal exporter may aggregate pages into page=0, but a
    # lone page=1 (or later) is always incomplete evidence.
    if page_number != 0:
        r2_status = "INCOMPLETE"
    elif page.get("hasMore") is True or next_cursor not in (None, "") or suspicious_gap_count:
        # The page is valid evidence, but it cannot prove complete inventory.
        r2_status = "INCOMPLETE"
    else:
        r2_status = "READY"
    output: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise RecoveryAuditError("invalid_r2_object")
        key = _text(item.get("key"), "r2_key", max_length=1_024)
        digest = _sha(item.get("sha256"), "r2_sha256")
        if not digest:
            raise RecoveryAuditError("r2_object_sha_required")
        output.append(
            {
                "key": key,
                "sha256": digest,
                "dataDate": _optional_date(item.get("dataDate", item.get("data_date")), "r2_data_date"),
                "domain": _optional_text(item.get("domain"), "r2_domain", max_length=80),
                "stage": _optional_text(item.get("stage"), "r2_stage", max_length=64),
                "bytes": _nonnegative_int(item.get("bytes", 0), "r2_bytes", maximum=10_000_000_000),
            }
        )
    return sorted(output, key=lambda item: item["key"]), r2_status


def _telemetry_metric(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        measured = value.get("value")
        measured_at = _optional_text(value.get("measuredAt", value.get("measured_at")), f"{field}_measured_at", max_length=64)
        reason = _optional_text(value.get("reason"), f"{field}_reason", max_length=512)
    else:
        measured = value
        measured_at = None
        reason = None
    if measured in (None, "", "unknown", "UNKNOWN"):
        return {"value": "unknown", "measuredAt": measured_at, "reason": reason or "not_measured"}
    if isinstance(measured, bool) or not isinstance(measured, (int, float)) or measured < 0:
        raise RecoveryAuditError(f"invalid_telemetry_{field}")
    return {"value": measured, "measuredAt": measured_at, "reason": reason}


def _telemetry(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = bundle.get("accountTelemetry") or {}
    if not isinstance(raw, Mapping):
        raise RecoveryAuditError("invalid_account_telemetry")
    return {field: _telemetry_metric(raw.get(field), field) for field in TELEMETRY_FIELDS}


def _limits(config: Mapping[str, Any]) -> dict[str, int | None]:
    limits = config.get("limits", {})
    storage = config.get("storage", {})
    if not isinstance(limits, Mapping) or not isinstance(storage, Mapping):
        raise RecoveryAuditError("invalid_budget_config")
    result: dict[str, int | None] = {}
    keys = {
        "d1RowsReadPerDay": limits.get("d1RowsReadPerDay"),
        "d1RowsWrittenPerDay": limits.get("d1RowsWrittenPerDay"),
        "workersRequestsPerDay": limits.get("workersRequestsPerDay"),
        "githubActionsMinutes": limits.get("githubActionsMinutesPerMonth"),
        "r2StorageBytes": storage.get("r2SoftBytes"),
    }
    for field, value in keys.items():
        if value is None:
            result[field] = None
        else:
            result[field] = _nonnegative_int(value, f"limit_{field}")
    # R2 operation limits are not in the checked-in budget contract.  Keep
    # them explicitly unknown instead of inventing a provider limit.
    result["r2ClassA"] = None
    result["r2ClassB"] = None
    return result


def _stage_budget_key(stage: str) -> str:
    # These are conservative mappings to existing resource-budget stages.  A
    # future config may provide exact canonical keys; it will be preferred.
    return {
        "core_collection": "coreCollection",
        "stock_flow": "coreCollection",
        "dashboard": "publish",
        "etf_penetration": "pcfExact",
        "pcf_exact": "pcfExact",
        "publish": "publish",
    }[stage]


def _read_only_inventory_budget(
    *,
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the finite resource envelope of this inventory run.

    Account-wide provider telemetry is useful when it is available, but it is
    not evidence about whether this read-only task respected its own bounds.
    R0-D therefore closes against exact task counters plus the workflow's hard
    timeout.  Missing counters, counter disagreement, or any exceeded cap fail
    closed.
    """

    raw_limits = config.get("readOnlyInventoryBudget")
    if not isinstance(raw_limits, Mapping):
        raise RecoveryAuditError("read_only_inventory_budget_required")
    limits = {
        field: _nonnegative_int(raw_limits.get(field), f"read_only_budget_{field}")
        for field in (*READ_ONLY_INVENTORY_USAGE_FIELDS, "actionsMinutesUpperBound")
    }
    resource_usage = bundle.get("resourceUsage")
    actual_source = resource_usage.get("actual") if isinstance(resource_usage, Mapping) else None
    if not isinstance(actual_source, Mapping):
        actual_source = {}
    actual: dict[str, int | str] = {}
    unknowns: list[str] = []
    violations: list[str] = []
    for field in READ_ONLY_INVENTORY_USAGE_FIELDS:
        value = actual_source.get(field)
        if value in (None, "", "unknown", "UNKNOWN"):
            actual[field] = "unknown"
            unknowns.append(f"local.{field}")
            continue
        measured = _nonnegative_int(value, f"local_{field}")
        actual[field] = measured
        if measured > limits[field]:
            violations.append(f"local.{field}:over_limit")

    d1_audit = bundle.get("d1Audit")
    d1_audit = d1_audit if isinstance(d1_audit, Mapping) else {}
    audit_fields = {
        "d1RowsRead": "rowsRead",
        "d1RowsWritten": "rowsWritten",
        "workerRequests": "workersRequests",
    }
    for local_field, audit_field in audit_fields.items():
        audit_value = d1_audit.get(audit_field)
        if audit_value in (None, "", "unknown", "UNKNOWN"):
            unknowns.append(f"local.d1Audit.{audit_field}")
            continue
        audited = _nonnegative_int(audit_value, f"local_d1_audit_{audit_field}")
        if actual[local_field] != "unknown" and actual[local_field] != audited:
            violations.append(f"local.{local_field}:counter_mismatch")

    # These two resources must remain mutation-free, not merely below a
    # positive allowance.  Keeping explicit assertions prevents a future
    # config edit from silently permitting writes in a read-only phase.
    if isinstance(actual["d1RowsWritten"], int) and actual["d1RowsWritten"] != 0:
        violations.append("local.d1RowsWritten:read_only_violation")
    if isinstance(actual["r2ClassA"], int) and actual["r2ClassA"] != 0:
        violations.append("local.r2ClassA:read_only_violation")

    if violations:
        decision = "over_budget"
    elif unknowns:
        decision = "unknown_metrics"
    else:
        decision = "allow"
    return {
        "decision": decision,
        "actual": actual,
        "limits": limits,
        "unknownMetrics": sorted(set(unknowns)),
        "violations": sorted(set(violations)),
        # The workflow test pins timeout-minutes to this value.  It is a hard
        # upper bound, not a fabricated post-run usage measurement.
        "actionsMinutesUpperBound": limits["actionsMinutesUpperBound"],
    }


def _budget_report(
    *,
    bundle: Mapping[str, Any],
    config: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    r2_status: str,
    phase: str,
) -> dict[str, Any]:
    telemetry = _telemetry(bundle)
    stage_budgets = config.get("stageBudgets")
    if not isinstance(stage_budgets, Mapping):
        raise RecoveryAuditError("stage_budget_config_required")
    retry_attempts = bundle.get("retryAttempts", 3)
    retry_attempts = _nonnegative_int(retry_attempts, "retry_attempts", maximum=3)
    if retry_attempts == 0:
        raise RecoveryAuditError("retry_attempts_must_be_positive")
    estimate: dict[str, int | str] = {
        "d1RowsRead": 0,
        "d1RowsWritten": 0,
        "workersRequests": 0,
        "actionsMinutes": "unknown",
        "r2StorageBytes": "unknown",
        "r2ClassA": "unknown",
        "r2ClassB": "unknown",
    }
    unknown_estimates: list[str] = []
    for target in targets:
        for stage in target["requiredStages"]:
            key = _stage_budget_key(stage)
            budget = stage_budgets.get(key)
            if not isinstance(budget, Mapping):
                unknown_estimates.append(f"stageBudgets.{key}")
                continue
            read = budget.get("rowsRead")
            written = budget.get("rowsWritten")
            if not isinstance(read, int) or read < 0 or not isinstance(written, int) or written < 0:
                raise RecoveryAuditError("invalid_stage_budget", key)
            estimate["d1RowsRead"] = int(estimate["d1RowsRead"]) + read * retry_attempts
            estimate["d1RowsWritten"] = int(estimate["d1RowsWritten"]) + written * retry_attempts
            estimate["workersRequests"] = int(estimate["workersRequests"]) + retry_attempts
    provided_estimate = bundle.get("recoveryEstimate")
    if isinstance(provided_estimate, Mapping):
        for field in ("actionsMinutes", "r2StorageBytes", "r2ClassA", "r2ClassB"):
            value = provided_estimate.get(field)
            if isinstance(value, int) and value >= 0:
                estimate[field] = value * retry_attempts
            elif value not in (None, "", "unknown", "UNKNOWN"):
                raise RecoveryAuditError(f"invalid_recovery_estimate_{field}")
    elif provided_estimate is not None:
        raise RecoveryAuditError("invalid_recovery_estimate")
    account_unknown_metrics = [
        field
        for field, metric in telemetry.items()
        if metric["value"] == "unknown"
    ]
    account_unknown_metrics.extend(unknown_estimates)
    if r2_status != "READY":
        account_unknown_metrics.append("r2Inventory")
    d1_audit = bundle.get("d1Audit") or {}
    if not isinstance(d1_audit, Mapping):
        raise RecoveryAuditError("invalid_d1_audit")
    measured = {
        "d1RowsRead": d1_audit.get("rowsRead", d1_audit.get("rows_read", "unknown")),
        "d1RowsWritten": d1_audit.get("rowsWritten", d1_audit.get("rows_written", "unknown")),
        "workersRequests": d1_audit.get("workersRequests", d1_audit.get("workers_requests", "unknown")),
    }
    for field, value in measured.items():
        if value in (None, "", "unknown", "UNKNOWN"):
            account_unknown_metrics.append(f"measured.{field}")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecoveryAuditError(f"invalid_measured_{field}")
    projected: dict[str, int | str] = {}
    for field in ("d1RowsRead", "d1RowsWritten", "workersRequests"):
        if measured[field] in ("unknown", None, ""):
            projected[field] = "unknown"
        else:
            projected[field] = int(measured[field]) + int(estimate[field])
    telemetry_field_map = {
        "actionsMinutes": "githubActionsMinutes",
        "r2StorageBytes": "r2StorageBytes",
        "r2ClassA": "r2ClassA",
        "r2ClassB": "r2ClassB",
    }
    for field, telemetry_field in telemetry_field_map.items():
        projected[field] = "unknown"
        if telemetry.get(telemetry_field, {}).get("value") not in (None, "unknown") and estimate[field] != "unknown":
            projected[field] = telemetry[telemetry_field]["value"] + int(estimate[field])
    limits = _limits(config)
    over_limit: list[str] = []
    limit_field_map = {
        "d1RowsRead": "d1RowsReadPerDay",
        "d1RowsWritten": "d1RowsWrittenPerDay",
        "workersRequests": "workersRequestsPerDay",
        "actionsMinutes": "githubActionsMinutes",
        "r2StorageBytes": "r2StorageBytes",
        "r2ClassA": "r2ClassA",
        "r2ClassB": "r2ClassB",
    }
    for field, limit_field in limit_field_map.items():
        value = projected.get(field)
        limit = limits.get(limit_field)
        if isinstance(value, int) and isinstance(limit, int) and value > limit:
            over_limit.append(field)
    local_budget = _read_only_inventory_budget(bundle=bundle, config=config)
    account_evidence = bundle.get("accountTelemetryEvidence")
    account_evidence_status = (
        str(account_evidence.get("status")).upper()
        if isinstance(account_evidence, Mapping) and account_evidence.get("status") is not None
        else "UNKNOWN"
    )
    if account_evidence_status == "VALID":
        account_captured_at = (
            account_evidence.get("capturedAt", account_evidence.get("captured_at"))
            if isinstance(account_evidence, Mapping)
            else None
        )
        if not _valid_instant(account_captured_at):
            account_unknown_metrics.append("accountTelemetryEvidenceCapturedAt")
        bundle_captured_at = bundle.get("capturedAt", bundle.get("captured_at"))
        if _valid_instant(account_captured_at) and _valid_instant(bundle_captured_at):
            captured_dt = datetime.fromisoformat(str(account_captured_at).replace("Z", "+00:00"))
            bundle_dt = datetime.fromisoformat(str(bundle_captured_at).replace("Z", "+00:00"))
            if captured_dt.tzinfo is None:
                captured_dt = captured_dt.astimezone()
            if bundle_dt.tzinfo is None:
                bundle_dt = bundle_dt.astimezone()
            if abs(bundle_dt - captured_dt) > timedelta(hours=1):
                account_unknown_metrics.append("accountTelemetryEvidenceStatus:EXPIRED")
    if over_limit:
        account_decision = "over_budget"
    elif account_unknown_metrics:
        account_decision = "unknown_metrics"
    else:
        account_decision = "allow"
    account_verified = account_evidence_status == "VALID" and account_decision == "allow"
    if phase == "R0-D":
        # R0-D is the production reconciliation gate.  The bounded local
        # envelope proves this read-only run stayed within its own caps, but
        # it cannot prove that the account has room for a later recovery.  A
        # missing/expired official account snapshot therefore remains an
        # UNKNOWN budget result and must not be upgraded to R0_RECONCILED.
        unknown_metrics = list(local_budget["unknownMetrics"])
        unknown_metrics.extend(account_unknown_metrics)
        hard_over_limit = list(local_budget["violations"])
        hard_over_limit.extend(over_limit)
        if account_decision == "over_budget" or local_budget["decision"] == "over_budget":
            decision = "over_budget"
            evidence_mode = "UNVERIFIED"
        elif account_decision != "allow" or account_evidence_status != "VALID":
            decision = "unknown_metrics"
            if account_evidence_status != "VALID":
                unknown_metrics.append("accountTelemetryEvidenceStatus")
            evidence_mode = "UNVERIFIED"
        elif local_budget["decision"] != "allow":
            decision = local_budget["decision"]
            evidence_mode = "UNVERIFIED"
        else:
            decision = "allow"
            evidence_mode = "ACCOUNT_VERIFIED" if account_verified else "UNVERIFIED"
    else:
        # Preserve R0-C's original account-aware draft semantics.
        decision = account_decision
        unknown_metrics = sorted(set(account_unknown_metrics))
        hard_over_limit = sorted(over_limit)
        evidence_mode = "ACCOUNT_VERIFIED" if account_verified and decision == "allow" else "UNVERIFIED"
    return {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "decision": decision,
        "evidenceMode": evidence_mode,
        "retryAttempts": retry_attempts,
        "accountTelemetry": telemetry,
        "accountTelemetryEvidenceStatus": account_evidence_status,
        "accountDecision": account_decision,
        "accountUnknownMetrics": sorted(set(account_unknown_metrics)),
        "accountOverLimit": sorted(over_limit),
        "localBudget": local_budget,
        "d1Audit": dict(d1_audit),
        "estimate": estimate,
        "measured": measured,
        "projected": projected,
        "limits": limits,
        "unknownMetrics": sorted(set(unknown_metrics)),
        "overLimit": sorted(set(hard_over_limit)),
        "r2InventoryStatus": r2_status,
        "policyTargets": {
            key: config.get("targets", {}).get(key)
            for key in (
                "d1RowsReadPerDay",
                "d1RowsWrittenPerDay",
                "workersRequestsPerDay",
                "githubActionsMinutesPerMonth",
            )
            if isinstance(config.get("targets"), Mapping) and isinstance(config["targets"].get(key), int)
        },
    }


def _target_stages(
    *,
    data_date: str,
    rows: Sequence[Mapping[str, Any]],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
) -> tuple[list[str], list[str], list[str]]:
    date_rows = [row for row in rows if row["dataDate"] == data_date]
    required_rows = [row for row in date_rows if row["required"]]
    applicability = [row["applicability"] for row in required_rows]
    unknowns: list[str] = []
    if any(value == "UNKNOWN" for value in applicability):
        unknowns.append("applicability_unknown")
    if any(value not in {"REQUIRED", "OFFICIAL_CLOSED", "NOT_APPLICABLE", "UNKNOWN"} for value in applicability):
        unknowns.append("applicability_invalid")
    if required_rows and all(value in {"OFFICIAL_CLOSED", "NOT_APPLICABLE"} for value in applicability):
        return [], ["officially_closed_or_not_applicable"], unknowns
    if not required_rows:
        return [], ["no_required_domain"], unknowns
    if unknowns:
        return [], unknowns, unknowns
    # Closed/not-applicable domains do not contribute rows to a required
    # stage.  Mixing them into the aggregate would turn a valid calendar
    # closure into an unexplained stage gap.
    applicable_rows = [row for row in required_rows if row["applicability"] == "REQUIRED"]
    stages: list[str] = []
    reasons: list[str] = []
    for stage in ("raw", "normalized", "model"):
        aggregate = _aggregate_stage(applicable_rows, stage)
        if aggregate in {"UNKNOWN", "BLOCKED"}:
            unknowns.append(f"{stage}:{aggregate}")
        elif aggregate == "SOURCE_UNAVAILABLE":
            reasons.append(f"{stage}_source_unavailable")
        elif aggregate != "READY":
            stages.append(RECOVERY_STAGE_MAP[stage])
            reasons.append(f"{stage}_{aggregate.lower()}")
    for stage in ("dashboard", "etf_penetration"):
        aggregate = _global_slot_status(data_date, stage, rows=rows, releases=releases)
        if aggregate in {"UNKNOWN", "BLOCKED"}:
            unknowns.append(f"{stage}:UNKNOWN")
        elif aggregate not in {"READY", "NOT_APPLICABLE"}:
            stages.append(RECOVERY_STAGE_MAP[stage])
            reasons.append(f"{stage}_{aggregate.lower()}")
    visible = _date_visible_status(data_date, rows=rows, releases=releases)
    if visible in {"UNKNOWN", "BLOCKED"}:
        unknowns.append("date_visible:UNKNOWN")
    elif visible not in {"READY", "NOT_APPLICABLE"}:
        stages.append("publish")
        reasons.append(f"date_visible_{visible.lower()}")
    # Preserve controlled stage order and de-duplicate reasons.  A plan may
    # still contain a stage only once even when several domains need it.
    ordered = [stage for stage in ("core_collection", "stock_flow", "pcf_exact", "dashboard", "etf_penetration", "publish") if stage in stages]
    return ordered, sorted(set(reasons)), sorted(set(unknowns))


def _source_unavailable_provenance(row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return whether a SOURCE_UNAVAILABLE row is a closed, auditable fact.

    A source outage is not a missing fact when its business date, reason,
    source fingerprint and an evidence receipt are present.  Keeping this
    predicate separate from the display record prevents a known outage from
    being confused with an unexplained gap while still failing closed for
    hand-written or incomplete evidence.
    """

    missing: list[str] = []
    if not row.get("sourceId"):
        missing.append("sourceId")
    if not row.get("sourceBusinessDate"):
        missing.append("sourceBusinessDate")
    if not row.get("gapReason"):
        missing.append("reason")
    if not row.get("sourceFingerprint"):
        missing.append("sourceFingerprint")
    if not row.get("evidenceSource"):
        missing.append("evidenceSource")
    if not _valid_instant(row.get("evidenceCapturedAt")):
        missing.append("evidenceCapturedAt")
    return not missing, missing


def _source_unavailable(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if row["applicability"] == "OFFICIAL_CLOSED":
            continue
        for stage, status in row["stages"].items():
            if status != "SOURCE_UNAVAILABLE":
                continue
            provenance_complete, missing = _source_unavailable_provenance(row)
            output.append(
                {
                    "dataDate": row["dataDate"],
                    "domain": row["domain"],
                    "sourceId": row["sourceId"],
                    "sourceBusinessDate": row["sourceBusinessDate"],
                    "stage": stage,
                    "reason": row["gapReason"] or "source_unavailable",
                    "evidenceSource": row["evidenceSource"],
                    "evidenceCapturedAt": row["evidenceCapturedAt"],
                    "sourceFingerprint": row["sourceFingerprint"],
                    "rawKey": row["rawKey"],
                    "rawSha256": row["rawSha256"],
                    "evidence": row["evidence"],
                    "provenanceStatus": "COMPLETE" if provenance_complete else "INCOMPLETE",
                    "missingProvenance": missing,
                }
            )
    return output


def _plan(
    *,
    bundle: Mapping[str, Any],
    start: str,
    end: str,
    priority: Sequence[str],
    dates: Sequence[str],
    domains: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    releases: Mapping[tuple[str, str], list[dict[str, Any]]],
    pipeline_windows: Sequence[Mapping[str, Any]],
    r2_status: str,
    budget: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    targets: list[dict[str, Any]] = []
    date_summaries: list[dict[str, Any]] = []
    for data_date in dates:
        date_rows = [row for row in rows if row["dataDate"] == data_date]
        stage_summary = {
            "raw": _aggregate_stage(date_rows, "raw"),
            "normalized": _aggregate_stage(date_rows, "normalized"),
            "model": _aggregate_stage(date_rows, "model"),
            "pcf_exact": _aggregate_stage(date_rows, "pcf_exact"),
            "dashboard": _global_slot_status(data_date, "dashboard", rows=rows, releases=releases),
            "etf_penetration": _global_slot_status(data_date, "etf_penetration", rows=rows, releases=releases),
            "date_visible": _date_visible_status(data_date, rows=rows, releases=releases),
        }
        required_stages, reasons, unknowns = _target_stages(
            data_date=data_date,
            rows=rows,
            releases=releases,
        )
        targetability = "ELIGIBLE" if required_stages and not unknowns else "VERIFY_FIRST" if unknowns else "NO_ACTION"
        if required_stages and targetability == "ELIGIBLE":
            targets.append(
                {
                    "dataDate": data_date,
                    "requiredStages": required_stages,
                    "reason": reasons or ["stage_gap"],
                    "dependsOn": sorted(
                        {
                            stage
                            for stage in ("raw", "normalized", "model", "pcf_exact")
                            if stage_summary[stage] != "READY"
                        }
                    ),
                }
            )
        date_summaries.append(
            {
                "dataDate": data_date,
                "priority": data_date in priority,
                "domainCount": len(date_rows),
                "applicability": dict(Counter(row["applicability"] for row in date_rows)),
                "stageStatus": stage_summary,
                "requiredStages": required_stages,
                "targetability": targetability,
                "reasons": reasons,
                "unknowns": unknowns,
            }
        )
    canonical_scope = {
        "startDate": start,
        "endDateInclusive": end,
        "priorityDates": list(priority),
        "calendarDaysInclusive": len(dates),
    }
    release_evidence = sorted(
        (dict(item) for item in (bundle.get("releaseManifests") or []) if isinstance(item, Mapping)),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    r2_evidence = sorted(
        (dict(item) for item in (bundle.get("r2Objects") or []) if isinstance(item, Mapping)),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    source_fingerprint = canonical_sha256(
        {
            "inventory": [_row_hash_payload(row) for row in rows],
            "releaseManifests": release_evidence,
            "pipelineWindows": list(pipeline_windows),
            "r2Inventory": bundle.get("r2Inventory") or {},
            "r2Objects": r2_evidence,
        }
    )
    plan_payload: dict[str, Any] = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "contractVersion": AUDIT_CONTRACT_VERSION,
        "mode": "R0_READ_ONLY_DRAFT",
        "status": "NOT_APPROVED",
        "executionBlocked": True,
        "campaignId": _text(bundle.get("campaignId"), "campaign_id", max_length=120),
        "scope": canonical_scope,
        "domains": [dict(domain) for domain in domains],
        "targets": targets,
        "dateSummaries": date_summaries,
        "pipelineWindows": list(pipeline_windows),
        "sourceFingerprint": source_fingerprint,
        "r2InventoryStatus": r2_status,
        "budgetDecision": budget["decision"],
        "requiredApproval": "批准恢复计划 <planHash> 后才允许 R2/R3",
    }
    plan_payload["planHash"] = canonical_sha256(plan_payload)
    return plan_payload, targets, date_summaries


def _summary(
    *,
    phase: str,
    plan: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    budget: Mapping[str, Any],
    source_unavailable: Sequence[Mapping[str, Any]],
    pipeline_windows: Sequence[Mapping[str, Any]],
    r2_status: str,
) -> str:
    counts = Counter(row["applicability"] for row in rows)
    unknowns = sum(
        len(summary.get("unknowns") or [])
        for summary in plan.get("dateSummaries", [])
        if isinstance(summary, Mapping)
    )
    unprovenanced = any(
        item.get("provenanceStatus") != "COMPLETE"
        for item in source_unavailable
        if isinstance(item, Mapping)
    )
    status = (
        "BLOCKED"
        if budget["decision"] == "over_budget"
        else "DONE_WITH_CONCERNS"
        if budget["decision"] == "unknown_metrics" or unknowns or r2_status != "READY" or unprovenanced
        else "DONE"
    )
    lines = [
        f"# Controlled recovery {phase} audit",
        "",
        f"STATUS: {status}",
        f"campaignId: `{plan['campaignId']}`",
        f"scope: `{plan['scope']['startDate']}..{plan['scope']['endDateInclusive']}` (inclusive)",
        f"planHash: `{plan['planHash']}` (NOT_APPROVED; executionBlocked=true)",
        f"sourceFingerprint: `{plan['sourceFingerprint']}`",
        "",
        "## Evidence counts",
        "",
        f"- matrix rows: {len(rows)}",
        f"- applicability: {dict(sorted(counts.items()))}",
        f"- recovery targets: {len(targets)}",
        f"- source-unavailable records: {len(source_unavailable)}",
        f"- pipeline windows: {len(pipeline_windows)}",
        f"- R2 inventory: {r2_status}",
        f"- budget decision: {budget['decision']}",
        f"- budget evidence mode: {budget['evidenceMode']}",
        f"- account budget advisory: {budget['accountDecision']}",
        "",
        "## Safety assertions",
        "",
        "- D1 writes: 0 (offline input contract; no D1 client is used)",
        "- R2 writes: 0 (offline input contract; no provider client is used)",
        "- workflow dispatches: 0",
        "- active pointer/feature flag changes: 0",
        "- protected paths: not opened by the auditor",
        "",
        "## Next action",
        "",
        "- Unknown evidence is not a recovery target; it requires R0-D verification.",
        "- Do not register the plan, enable a flag, run migration, dispatch a workflow, or run a canary from this output.",
    ]
    if source_unavailable:
        lines.extend(["", "## Source unavailable", ""])
        lines.extend(
            f"- {item['dataDate']} / {item['domain']} / {item['stage']}: {item['reason']}"
            for item in source_unavailable
        )
    if budget["unknownMetrics"]:
        lines.extend(["", "## Unknown budget metrics", "", f"- {', '.join(budget['unknownMetrics'])}"])
    if budget["accountUnknownMetrics"] and budget["evidenceMode"] == "LOCAL_BOUNDED":
        lines.extend(
            [
                "",
                "## Optional account telemetry",
                "",
                "- Account-wide usage is unavailable or incomplete; R0-D closed only against the bounded read-only task envelope.",
                f"- Advisory unknowns: {', '.join(budget['accountUnknownMetrics'])}",
                "- Recovery and backfill remain subject to their separate account-level budget gate.",
            ]
        )
    return "\n".join(lines) + "\n"


READ_ONLY_ASSERTION_FIELDS = (
    "d1Writes",
    "r2Writes",
    "workflowDispatches",
    "activePointerChanges",
    "flagChanges",
)


def _runtime_safety_evidence(
    bundle: Mapping[str, Any],
    *,
    phase: str,
) -> tuple[dict[str, int], list[str], list[str], list[str]]:
    """Read safety assertions without granting absent live evidence a pass."""

    runtime = bundle.get("runtimeEvidence")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    raw_assertions = bundle.get("readOnlyAssertions")
    if raw_assertions is None:
        raw_assertions = runtime.get("readOnlyAssertions")
    unknowns: list[str] = []
    blockers: list[str] = []
    if raw_assertions is None:
        if phase == "R0-D":
            unknowns.append("readOnlyAssertions:UNKNOWN")
        assertions = {field: 0 for field in READ_ONLY_ASSERTION_FIELDS}
    elif not isinstance(raw_assertions, Mapping):
        raise RecoveryAuditError("invalid_read_only_assertions")
    else:
        assertions = {}
        for field in READ_ONLY_ASSERTION_FIELDS:
            value = raw_assertions.get(field)
            if value in (None, "", "unknown", "UNKNOWN"):
                if phase == "R0-D":
                    unknowns.append(f"readOnlyAssertions:{field}:UNKNOWN")
                assertions[field] = 0
                continue
            assertions[field] = _nonnegative_int(value, f"read_only_{field}")
            if assertions[field] != 0:
                blockers.append(f"readOnlyAssertions:{field}={assertions[field]}")

    raw_paths = bundle.get("protectedPathsTouched")
    if raw_paths is None:
        raw_paths = runtime.get("protectedPathsTouched")
    if raw_paths is None:
        protected_paths: list[str] = []
        if phase == "R0-D":
            unknowns.append("protectedPathsTouched:UNKNOWN")
    else:
        protected_paths = _list_of_text(raw_paths, "protected_paths_touched", max_count=100)
        blockers.extend(f"protectedPathTouched:{path}" for path in protected_paths)
    return assertions, unknowns, blockers, protected_paths


def _required_stage_unknowns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    unknowns: list[str] = []
    for row in rows:
        if not row.get("required") or row.get("applicability") != "REQUIRED":
            continue
        for stage, status in row.get("stages", {}).items():
            if status in {"UNKNOWN", "PENDING"}:
                unknowns.append(f"{row['dataDate']}:{row['domain']}:{stage}:{status}")
    return unknowns


def evaluate_r0d_closure(
    *,
    phase: str,
    status: str,
    contract_version: str,
    unknowns: Sequence[str],
    blockers: Sequence[str],
    protected_paths: Sequence[str],
    read_only_assertions: Mapping[str, Any],
    budget: Mapping[str, Any],
    r2_status: str,
    rows: Sequence[Mapping[str, Any]],
    source_unavailable: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate the R0-D closure predicate without side effects.

    The auditor deliberately computes ordinary execution status separately
    from this predicate.  ``DONE`` means that the local function completed;
    only a ``True`` result here may advance R0-D to ``R0_RECONCILED``.  The
    returned failure lists are deterministic and can be fed back into the
    stage-evidence payload without changing the canonical plan hash.
    """

    if phase == "R0-C":
        blocked = status == "BLOCKED"
        return {
            "closed": False,
            "reconciliationStatus": "PENDING" if blocked else "NOT_APPLICABLE",
            "nextAllowedAction": "STOP" if blocked else "R0-D_RECONCILE",
            # R0-C cannot reconcile production, but it must not discard the
            # evidence that made the local run concerned or blocked.  Keep the
            # lists canonical so callers can use this pure function directly.
            "unknowns": sorted(set(unknowns)),
            "blockers": sorted(set(blockers)),
        }

    predicate_unknowns = list(unknowns)
    predicate_blockers = list(blockers)

    def require(value: bool, reason: str, *, blocking: bool = False) -> None:
        if value:
            return
        (predicate_blockers if blocking else predicate_unknowns).append(reason)

    require(phase == "R0-D", "phase:not_R0-D", blocking=True)
    require(contract_version == AUDIT_CONTRACT_VERSION, "contractVersion:mismatch", blocking=True)
    require(status == "DONE", f"status:{status}")
    require(not protected_paths, "protectedPathsTouched:non_empty", blocking=True)

    for field in READ_ONLY_ASSERTION_FIELDS:
        value = read_only_assertions.get(field) if isinstance(read_only_assertions, Mapping) else None
        if value in (None, "unknown", "UNKNOWN"):
            require(False, f"readOnlyAssertions:{field}:unknown")
        else:
            require(
                value == 0 and not isinstance(value, bool),
                f"readOnlyAssertions:{field}:not_zero",
                blocking=True,
            )

    require(budget.get("decision") == "allow", f"budgetStatus:{budget.get('decision', 'unknown')}")
    require(budget.get("evidenceMode") == "ACCOUNT_VERIFIED", "budgetEvidenceMode:not_account_verified")
    require(budget.get("accountDecision") == "allow", f"accountBudgetStatus:{budget.get('accountDecision', 'unknown')}")
    require(
        budget.get("accountTelemetryEvidenceStatus") == "VALID",
        "accountTelemetryEvidenceStatus:not_valid",
    )
    local_budget = budget.get("localBudget")
    require(
        isinstance(local_budget, Mapping) and local_budget.get("decision") == "allow",
        "localBudgetStatus:not_allow",
    )

    if r2_status == "BLOCKED":
        require(False, "r2Inventory:BLOCKED", blocking=True)
    else:
        require(r2_status == "READY", f"r2Inventory:{r2_status}")

    # Every required row must have a known applicability and a stage state
    # that is either ready, intentionally missing, or an evidenced source
    # unavailability.  Unknown/pending/invalid states are never closed.
    allowed_stage_states = {"MISSING", "SOURCE_UNAVAILABLE", "OFFICIAL_CLOSED", "NOT_APPLICABLE"}
    for row in rows:
        if not row.get("required"):
            continue
        row_key = f"{row.get('dataDate')}:{row.get('domain')}"
        applicability = row.get("applicability")
        if applicability == "UNKNOWN":
            require(False, f"{row_key}:applicability:UNKNOWN")
            continue
        if applicability != "REQUIRED":
            continue
        stages = row.get("stages") if isinstance(row.get("stages"), Mapping) else {}
        for stage in AUDIT_STAGES:
            stage_status = str(stages.get(stage, "UNKNOWN"))
            if stage_status in {"UNKNOWN", "PENDING"}:
                require(False, f"{row_key}:{stage}:{stage_status}")
            elif stage_status in {"BLOCKED", "BLOCKED_QUALITY", "INVALID"}:
                require(False, f"{row_key}:{stage}:{stage_status}", blocking=True)
            elif not (_is_ready(stage_status) or stage_status in allowed_stage_states):
                require(False, f"{row_key}:{stage}:unrecognized")

    for item in source_unavailable:
        if item.get("provenanceStatus") != "COMPLETE":
            require(
                False,
                f"{item.get('dataDate')}:{item.get('domain')}:{item.get('stage')}:source_provenance:INCOMPLETE",
            )

    require(plan.get("status") == "NOT_APPROVED", "planStatus:not_NOT_APPROVED", blocking=True)
    require(plan.get("executionBlocked") is True, "planExecutionBlocked:false", blocking=True)
    require(plan.get("contractVersion") == AUDIT_CONTRACT_VERSION, "planContractVersion:mismatch", blocking=True)
    require(
        isinstance(plan.get("planHash"), str) and SHA256_RE.fullmatch(plan["planHash"]) is not None,
        "planHash:invalid",
        blocking=True,
    )
    require(
        isinstance(plan.get("sourceFingerprint"), str)
        and SHA256_RE.fullmatch(plan["sourceFingerprint"]) is not None,
        "sourceFingerprint:invalid",
        blocking=True,
    )

    predicate_unknowns = sorted(set(predicate_unknowns))
    predicate_blockers = sorted(set(predicate_blockers))
    closed = not predicate_unknowns and not predicate_blockers
    if closed:
        reconciliation_status = "R0_RECONCILED"
        next_action = "R2_ACCEPTANCE_AUDIT"
    else:
        reconciliation_status = "PENDING"
        next_action = "STOP" if predicate_blockers else "R0-D_RECONCILE"
    return {
        "closed": closed,
        "reconciliationStatus": reconciliation_status,
        "nextAllowedAction": next_action,
        "unknowns": predicate_unknowns,
        "blockers": predicate_blockers,
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_stage_evidence(
    *,
    phase: str,
    bundle: Mapping[str, Any],
    plan: Mapping[str, Any],
    budget: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    pipeline_windows: Sequence[Mapping[str, Any]],
    r2_status: str,
    source_unavailable: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    unknowns: list[str] = list(budget["unknownMetrics"])
    if r2_status != "READY":
        unknowns.append(f"r2Inventory:{r2_status}")
    unknowns.extend(_required_stage_unknowns(rows))
    unknowns.extend(
        f"{row['dataDate']}:{row['domain']}:applicability:UNKNOWN"
        for row in rows
        if row.get("required") and row.get("applicability") == "UNKNOWN"
    )
    # The date summaries include global slot and date-visible evidence, which
    # cannot be inferred from a single domain row.
    unknowns.extend(
        f"{summary['dataDate']}:{item}"
        for summary in plan.get("dateSummaries", [])
        if isinstance(summary, Mapping)
        for item in summary.get("unknowns", [])
    )
    blockers = list(budget["overLimit"])
    blockers.extend(
        f"{row['dataDate']}:{row['domain']}:{stage}:BLOCKED"
        for row in rows
        if row.get("required") and row.get("applicability") == "REQUIRED"
        for stage, status in row["stages"].items()
        if status in {"BLOCKED", "BLOCKED_QUALITY", "INVALID"}
    )
    incomplete_source_unavailable = [
        item
        for item in source_unavailable
        if item.get("provenanceStatus") != "COMPLETE"
    ]
    unknowns.extend(
        f"{item['dataDate']}:{item['domain']}:{item['stage']}:source_provenance:{','.join(item.get('missingProvenance') or [])}"
        for item in incomplete_source_unavailable
    )
    assertions, safety_unknowns, safety_blockers, protected_paths = _runtime_safety_evidence(
        bundle,
        phase=phase,
    )
    unknowns.extend(safety_unknowns)
    blockers.extend(safety_blockers)
    runtime = bundle.get("runtimeEvidence")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    metrics_measured_at = runtime.get("metricsMeasuredAt", runtime.get("metrics_measured_at"))
    if phase == "R0-D" and not isinstance(metrics_measured_at, str):
        unknowns.append("metricsMeasuredAt:UNKNOWN")
    source_incomplete = bool(incomplete_source_unavailable) or r2_status != "READY" or bool(
        unknowns
    )
    status = "BLOCKED" if blockers else "DONE_WITH_CONCERNS" if unknowns else "DONE"
    closure = evaluate_r0d_closure(
        phase=phase,
        status=status,
        contract_version=AUDIT_CONTRACT_VERSION,
        unknowns=unknowns,
        blockers=blockers,
        protected_paths=protected_paths,
        read_only_assertions=assertions,
        budget=budget,
        r2_status=r2_status,
        rows=rows,
        source_unavailable=source_unavailable,
        plan=plan,
    )
    if phase == "R0-D":
        # The pure predicate may discover an invariant that is not represented
        # by a normal inventory warning (for example a malformed draft plan).
        # Preserve that evidence in the output before deriving the final
        # execution status and stop action.
        unknowns = sorted(set(closure["unknowns"]))
        blockers = sorted(set(closure["blockers"]))
        status = "BLOCKED" if blockers else "DONE_WITH_CONCERNS" if unknowns else "DONE"
        source_incomplete = bool(incomplete_source_unavailable) or r2_status != "READY" or bool(
            unknowns
        )
    reconciliation_status = closure["reconciliationStatus"]
    next_action = closure["nextAllowedAction"]
    if phase == "R0-C":
        # R0-C is a code/evidence validation phase only; it can never advance
        # directly to R2 even when its fixture happens to be complete.
        reconciliation_status = "PENDING" if status == "BLOCKED" else "NOT_APPLICABLE"
        next_action = "STOP" if status == "BLOCKED" else "R0-D_RECONCILE"
    checks = [
        {"id": "input_schema", "status": "pass", "detail": "input schemaVersion=1 accepted; output contract is v2"},
        {"id": "read_only_boundary", "status": "pass", "detail": "no network/provider client or mutation API is available"},
        {"id": "stable_plan_hash", "status": "pass", "detail": "planHash derives from canonical scope/evidence only"},
        {"id": "unknown_fail_closed", "status": "pass", "detail": "unknown evidence cannot become a recovery target or R0_RECONCILED"},
        {
            "id": "budget_gate",
            "status": "pass" if budget["decision"] == "allow" else "warn",
            "detail": f"{budget['decision']}:{budget['evidenceMode']}",
        },
        {"id": "release_slot_separation", "status": "pass", "detail": "dashboard and etf_penetration are evaluated independently"},
        {"id": "r0d_reconciliation", "status": "pass" if reconciliation_status == "R0_RECONCILED" else "warn", "detail": reconciliation_status},
    ]
    repository = runtime.get("repository") if isinstance(runtime, Mapping) else {}
    if not isinstance(repository, Mapping):
        repository = {}
    release_slots = {
        slot: sum(
            1
            for summary in plan.get("dateSummaries", [])
            if isinstance(summary, Mapping)
            and summary.get("stageStatus", {}).get(slot) == "READY"
        )
        for slot in ("dashboard", "etf_penetration")
    }
    if source_incomplete:
        source_status = "INCOMPLETE"
    elif source_unavailable:
        source_status = "READY_WITH_EVIDENCED_UNAVAILABLE"
    else:
        source_status = "READY"
    return {
        "schemaVersion": STAGE_EVIDENCE_SCHEMA_VERSION,
        "contractVersion": AUDIT_CONTRACT_VERSION,
        "phase": phase,
        "status": status,
        "reconciliationStatus": reconciliation_status,
        "generatedAt": bundle.get("capturedAt", "unknown"),
        "baseMainSha": repository.get("originMain", "unknown"),
        "headSha": repository.get("headSha", repository.get("originMain", "unknown")),
        "planHash": plan["planHash"],
        "targetDates": sorted({target["dataDate"] for target in targets}),
        "requiredStages": sorted({stage for target in targets for stage in target["requiredStages"]}),
        "migrationPlan": [],
        "migrationApplied": False,
        "deploySha": "unknown",
        "deployRun": None,
        "flagsBefore": {},
        "flagsAfter": {},
        "authorizationRefs": [],
        "sourceEvidenceStatus": source_status,
        "r2InventoryStatus": r2_status,
        "budgetStatus": budget["decision"],
        "budgetEvidenceMode": budget["evidenceMode"],
        "localBudgetStatus": budget["localBudget"]["decision"],
        "accountBudgetStatus": budget["accountDecision"],
        "accountTelemetryEvidenceStatus": budget["accountTelemetryEvidenceStatus"],
        "claimDecision": "NOT_ATTEMPTED",
        "recoveryId": None,
        "runKey": None,
        "leaseOwner": None,
        "attemptCount": 0,
        "collectorRun": None,
        "publishRun": None,
        "releaseSlots": release_slots,
        "receipts": [],
        "d1RowsRead": budget["measured"].get("d1RowsRead", "unknown"),
        "d1RowsWritten": budget["measured"].get("d1RowsWritten", "unknown"),
        "workerRequests": budget["measured"].get("workersRequests", "unknown"),
        "r2ClassA": "unknown",
        "r2ClassB": "unknown",
        "actionsMinutes": "unknown",
        "metricsMeasuredAt": metrics_measured_at if isinstance(metrics_measured_at, str) else None,
        "checks": checks,
        "unknowns": sorted(set(unknowns)),
        "blockers": sorted(set(blockers)),
        "nextAllowedAction": next_action,
        "protectedPathsTouched": protected_paths,
        "readOnlyAssertions": assertions,
        "pipelineWindowCount": len(pipeline_windows),
    }


def audit_bundle(
    bundle: Mapping[str, Any],
    *,
    budget_config: Mapping[str, Any],
    phase: str = "R0-C",
) -> dict[str, Any]:
    """Audit one local evidence bundle and return all deterministic outputs."""

    if phase not in AUDIT_PHASES:
        raise RecoveryAuditError("invalid_audit_phase", str(phase))
    if not isinstance(bundle, Mapping):
        raise RecoveryAuditError("bundle_must_be_object")
    _reject_secret_keys(bundle)
    if bundle.get("schemaVersion") != INPUT_SCHEMA_VERSION:
        raise RecoveryAuditError("unknown_audit_schema_version")
    start, end, priority, dates = _scope(bundle)
    domains = _domains(bundle)
    releases = _release_index(bundle)
    rows = _validate_inventory(bundle, dates=dates, domains=domains, releases=releases)
    pipeline_windows = _validate_pipeline_windows(bundle)
    r2_objects, r2_status = _validate_r2_objects(bundle)
    # Build a preliminary target set with a placeholder budget, then compute
    # the real bounded estimate and final plan/evidence.
    preliminary_budget = {"decision": "unknown_metrics", "unknownMetrics": [], "overLimit": [], "measured": {}}
    preliminary_plan, targets, _ = _plan(
        bundle=bundle,
        start=start,
        end=end,
        priority=priority,
        dates=dates,
        domains=domains,
        rows=rows,
        releases=releases,
        pipeline_windows=pipeline_windows,
        r2_status=r2_status,
        budget=preliminary_budget,
    )
    budget = _budget_report(
        bundle=bundle,
        config=budget_config,
        targets=targets,
        r2_status=r2_status,
        phase=phase,
    )
    plan, targets, date_summaries = _plan(
        bundle=bundle,
        start=start,
        end=end,
        priority=priority,
        dates=dates,
        domains=domains,
        rows=rows,
        releases=releases,
        pipeline_windows=pipeline_windows,
        r2_status=r2_status,
        budget=budget,
    )
    source_unavailable = _source_unavailable(rows)
    evidence = build_stage_evidence(
        phase=phase,
        bundle=bundle,
        plan=plan,
        budget=budget,
        rows=rows,
        targets=targets,
        pipeline_windows=pipeline_windows,
        r2_status=r2_status,
        source_unavailable=source_unavailable,
    )
    summary = _summary(
        phase=phase,
        plan=plan,
        rows=rows,
        targets=targets,
        budget=budget,
        source_unavailable=source_unavailable,
        pipeline_windows=pipeline_windows,
        r2_status=r2_status,
    )
    return {
        "inventory": rows,
        "plan": plan,
        "targets": targets,
        "dateSummaries": date_summaries,
        "sourceUnavailable": source_unavailable,
        "budget": budget,
        "stageEvidence": evidence,
        "summary": summary,
        "r2Objects": r2_objects,
        "pipelineWindows": pipeline_windows,
    }


def write_audit_outputs(result: Mapping[str, Any], output_dir: Path) -> None:
    """Write the seven bounded audit outputs below an isolated directory."""

    output_dir = output_dir.resolve()
    # Validate the complete destination before creating any directory.  This
    # keeps a typo such as ``reports/phase0/...`` side-effect free as well as
    # preventing an overwrite of the user's protected reports/memory paths.
    assert_recovery_output_path(str(output_dir / "_audit_output_guard"))
    output_dir.mkdir(parents=True, exist_ok=True)
    filenames = (
        "inventory.json",
        "inventory.csv",
        "recovery-plan.json",
        "source-unavailable.csv",
        "budget-estimate.json",
        "stage-evidence.json",
        "summary.md",
    )
    for filename in filenames:
        assert_recovery_output_path(str(output_dir / filename))
    inventory = list(result["inventory"])
    (output_dir / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    inventory_csv_rows = []
    for row in inventory:
        inventory_csv_rows.append(
            {
                "dataDate": row["dataDate"],
                "domain": row["domain"],
                "market": row["market"] or "",
                "applicability": row["applicability"],
                **{f"{stage}Status": row["stages"][stage] for stage in AUDIT_STAGES},
                "rawKey": row["rawKey"] or "",
                "rawSha256": row["rawSha256"] or "",
                "normalizedKey": row["normalizedKey"] or "",
                "normalizedSha256": row["normalizedSha256"] or "",
                "hashOrigin": row["hashOrigin"] or "",
                "gapReason": row["gapReason"] or "",
                "nextStage": row["nextStage"] or "",
            }
        )
    _write_csv(
        output_dir / "inventory.csv",
        inventory_csv_rows,
        [
            "dataDate",
            "domain",
            "market",
            "applicability",
            *[f"{stage}Status" for stage in AUDIT_STAGES],
            "rawKey",
            "rawSha256",
            "normalizedKey",
            "normalizedSha256",
            "hashOrigin",
            "gapReason",
            "nextStage",
        ],
    )
    (output_dir / "recovery-plan.json").write_text(
        json.dumps(result["plan"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(
        output_dir / "source-unavailable.csv",
        list(result["sourceUnavailable"]),
        [
            "dataDate",
            "domain",
            "sourceId",
            "sourceBusinessDate",
            "stage",
            "reason",
            "evidenceSource",
            "evidenceCapturedAt",
            "sourceFingerprint",
            "rawKey",
            "rawSha256",
            "evidence",
            "provenanceStatus",
            "missingProvenance",
        ],
    )
    (output_dir / "budget-estimate.json").write_text(
        json.dumps(result["budget"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "stage-evidence.json").write_text(
        json.dumps(result["stageEvidence"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(result["summary"], encoding="utf-8")
