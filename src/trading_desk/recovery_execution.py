"""Bounded recovery execution contracts.

The recovery path is deliberately plan driven and side-effect free in this
module.  A plan is an immutable description of *exactly* which dates and
stages may run.  The executor can use the returned matrix to iterate one date
and one stage at a time, while the receipt helpers provide a small, auditable
completion proof for the R2 control plane.

No function in this module talks to D1, R2, GitHub or an official source.
Remote orchestration belongs to the later, explicitly approved recovery
stage.  Keeping the contracts pure makes it possible to test retry, failure
and quota behaviour without spending production resources.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_CONTRACT_VERSION = "recovery-execution-v1"
MAX_PLAN_BYTES = 300_000
RUN_KEY_RE = re.compile(
    r"^daily-market-data:(?P<date>\d{4}-\d{2}-\d{2}):(?P<window>[a-z_]+):(?P<version>v\d+)$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z$"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9:_./-]{1,220}$")
RECEIPT_KEY_RE = re.compile(
    r"^market-data/recovery/receipts/[A-Za-z0-9._:-]{1,120}/"
    r"\d{4}-\d{2}-\d{2}/[a-z_]{1,64}/[0-9]{1,12}\.json$"
)

# These names are the only stages a recovery plan may request.  They map to
# existing read-model slots and workflow boundaries; a caller cannot invent
# a stage which the publisher does not know how to verify.
RECOVERY_STAGES = (
    "core_collection",
    "dashboard",
    "etf_penetration",
    "stock_flow",
    "pcf_exact",
    "publish",
)
RECOVERY_STAGE_SET = frozenset(RECOVERY_STAGES)
RECOVERY_STATUSES = (
    "COMPLETED",
    "FAILED_RETRYABLE",
    "BLOCKED_QUALITY",
    "SOURCE_UNAVAILABLE",
)
RECOVERY_STATUS_SET = frozenset(RECOVERY_STATUSES)


class RecoveryContractError(ValueError):
    """A plan, receipt or budget violates a recovery invariant."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        .encode("utf-8")
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(_canonical_bytes(value))


def plan_bytes(value: Mapping[str, Any]) -> tuple[bytes, str]:
    """Serialize an approved plan and return its immutable object SHA.

    ``planHash`` is the semantic hash inside the plan (the canonical plan
    without its self-referential hash field).  The recovery table's
    ``scope_sha256`` is the separate hash of the exact bytes uploaded to R2;
    keeping these two digests distinct lets the Worker verify both the
    meaning and the immutable object bytes.
    """

    validate_recovery_plan(value)
    encoded = _canonical_bytes(value) + b"\n"
    return encoded, sha256_bytes(encoded)


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecoveryContractError(code)
    return value


def _string(value: Any, field: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryContractError(f"invalid_{field}")
    text = value.strip()
    if pattern is not None and not pattern.fullmatch(text):
        raise RecoveryContractError(f"invalid_{field}")
    return text


def _hash(value: Any, field: str) -> str:
    text = _string(value, field).lower()
    if not SHA256_RE.fullmatch(text):
        raise RecoveryContractError(f"invalid_{field}")
    return text


def _date(value: Any, field: str) -> str:
    text = _string(value, field, pattern=ISO_DATE_RE)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise RecoveryContractError(f"invalid_{field}") from exc
    return text


def _instant(value: Any, field: str) -> str:
    text = _string(value, field, pattern=ISO_INSTANT_RE)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryContractError(f"invalid_{field}") from exc
    return text


def _nonnegative_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryContractError(f"invalid_{field}")
    if maximum is not None and value > maximum:
        raise RecoveryContractError(f"{field}_exceeds_limit")
    return value


def _date_list(value: Any, field: str, *, max_count: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise RecoveryContractError(f"invalid_{field}")
    dates = tuple(_date(item, f"{field}_item") for item in value)
    if len(dates) > max_count:
        raise RecoveryContractError(f"{field}_exceeds_limit")
    if len(set(dates)) != len(dates):
        raise RecoveryContractError(f"duplicate_{field}")
    if tuple(sorted(dates)) != dates:
        raise RecoveryContractError(f"{field}_must_be_ascending")
    return dates


def parse_run_key(run_key: Any) -> dict[str, str]:
    value = _string(run_key, "run_key")
    match = RUN_KEY_RE.fullmatch(value)
    if not match:
        raise RecoveryContractError("invalid_run_key")
    parsed_date = _date(match.group("date"), "run_key_date")
    window = match.group("window")
    if window not in {"evening", "morning", "morning_exact", "reconcile"}:
        raise RecoveryContractError("unknown_run_key_window")
    version = match.group("version")
    if version != "v1":
        raise RecoveryContractError("unknown_run_key_version")
    return {"date": parsed_date, "window": window, "version": version}


def _safe_isolated_path(value: Any, field: str) -> str:
    text = _string(value, field)
    if "\\" in text or text.startswith("/"):
        raise RecoveryContractError(f"invalid_{field}")
    path = PurePosixPath(text)
    if ".." in path.parts:
        raise RecoveryContractError(f"invalid_{field}")
    if text.startswith("reports/phase0") or text == "memory" or text.startswith("memory/"):
        raise RecoveryContractError(f"{field}_protected_path")
    # Recovery artifacts must be isolated from the user's root reports and
    # memory.  A relative path under one of these explicit roots is safe.
    if not (
        text == "recovery"
        or text.startswith("recovery/")
        or text == ".recovery"
        or text.startswith(".recovery/")
    ):
        raise RecoveryContractError(f"{field}_must_be_isolated")
    return text.rstrip("/")


def assert_recovery_output_path(path: str) -> None:
    """Reject CLI/report output that could overwrite protected user files."""

    parts = PurePosixPath(str(path)).parts
    if any(parts[index : index + 2] == ("reports", "phase0") for index in range(len(parts) - 1)):
        raise RecoveryContractError("recovery_output_protected_path")
    if "memory" in parts:
        raise RecoveryContractError("recovery_output_protected_path")


def _budget_manifest(value: Any) -> tuple[Any, str]:
    if isinstance(value, str):
        key = _string(value, "budget_manifest", pattern=IDENTIFIER_RE)
        if not (key.startswith("ops-budget/") or key.startswith("recovery/")):
            raise RecoveryContractError("budget_manifest_must_be_archived")
        return key, ""
    if isinstance(value, Mapping):
        # Inline manifests are useful for offline planning, but are still
        # hashed as part of the immutable plan.  They cannot contain a path
        # which points at protected root output.
        encoded = dict(value)
        return encoded, canonical_sha256(encoded)
    raise RecoveryContractError("invalid_budget_manifest")


@dataclass(frozen=True)
class RecoveryScope:
    campaign_id: str
    recovery_id: str
    plan_hash: str
    run_key: str
    data_dates: tuple[str, ...]
    required_stages: tuple[str, ...]
    artifact_root: str
    state_dir: str
    max_dates: int
    max_requests: int
    deadline: str
    budget_manifest: Any
    budget_manifest_sha256: str
    contract_version: str = RECOVERY_CONTRACT_VERSION

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schemaVersion": RECOVERY_SCHEMA_VERSION,
            "contractVersion": self.contract_version,
            "campaignId": self.campaign_id,
            "recoveryId": self.recovery_id,
            "runKey": self.run_key,
            "dataDates": list(self.data_dates),
            "requiredStages": list(self.required_stages),
            "artifactRoot": self.artifact_root,
            "stateDir": self.state_dir,
            "maxDates": self.max_dates,
            "maxRequests": self.max_requests,
            "deadline": self.deadline,
            "budgetManifest": self.budget_manifest,
        }
        if self.budget_manifest_sha256:
            value["budgetManifestSha256"] = self.budget_manifest_sha256
        if include_hash:
            value["planHash"] = self.plan_hash
        return value


def _plan_hash_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("planHash", None)
    payload.pop("plan_hash", None)
    return payload


def validate_recovery_plan(value: Mapping[str, Any], *, expected_plan_hash: str | None = None) -> RecoveryScope:
    payload = _require_mapping(value, "invalid_recovery_plan")
    schema = payload.get("schemaVersion", payload.get("schema_version"))
    if schema != RECOVERY_SCHEMA_VERSION:
        raise RecoveryContractError("unknown_recovery_schema_version")
    contract = payload.get("contractVersion", payload.get("contract_version"))
    if contract != RECOVERY_CONTRACT_VERSION:
        raise RecoveryContractError("unknown_recovery_contract_version")
    supplied_hash = payload.get("planHash", payload.get("plan_hash"))
    supplied_hash = _hash(supplied_hash, "plan_hash")
    computed_hash = canonical_sha256(_plan_hash_payload(payload))
    if supplied_hash != computed_hash:
        raise RecoveryContractError("plan_hash_mismatch")
    if expected_plan_hash is not None and supplied_hash != _hash(expected_plan_hash, "expected_plan_hash"):
        raise RecoveryContractError("approved_plan_hash_mismatch")
    campaign_id = _string(payload.get("campaignId", payload.get("campaign_id")), "campaign_id", pattern=IDENTIFIER_RE)
    recovery_id = _string(payload.get("recoveryId", payload.get("recovery_id")), "recovery_id", pattern=IDENTIFIER_RE)
    run_key = _string(payload.get("runKey", payload.get("run_key")), "run_key")
    parsed = parse_run_key(run_key)
    dates_value = payload.get("dataDates", payload.get("data_dates"))
    data_dates = _date_list(dates_value, "data_dates", max_count=3)
    if parsed["date"] not in data_dates and len(data_dates) == 1:
        # A one-day recovery must be bound to its business window.  Multi-day
        # campaigns may use one registered run key for their approved scope.
        raise RecoveryContractError("run_key_date_not_in_scope")
    stages_raw = payload.get("requiredStages", payload.get("required_stages"))
    if not isinstance(stages_raw, (list, tuple)) or not stages_raw:
        raise RecoveryContractError("invalid_required_stages")
    required_stages = tuple(_string(item, "required_stage") for item in stages_raw)
    if len(set(required_stages)) != len(required_stages):
        raise RecoveryContractError("duplicate_required_stage")
    unknown = set(required_stages) - RECOVERY_STAGE_SET
    if unknown:
        raise RecoveryContractError("unknown_required_stage")
    artifact_root = _safe_isolated_path(payload.get("artifactRoot", payload.get("artifact_root")), "artifact_root")
    state_dir = _safe_isolated_path(payload.get("stateDir", payload.get("state_dir")), "state_dir")
    max_dates = _nonnegative_int(payload.get("maxDates", payload.get("max_dates")), "max_dates", maximum=3)
    if max_dates < len(data_dates) or max_dates == 0:
        raise RecoveryContractError("max_dates_inconsistent")
    max_requests = _nonnegative_int(payload.get("maxRequests", payload.get("max_requests")), "max_requests", maximum=10_000)
    if max_requests == 0:
        raise RecoveryContractError("max_requests_must_be_positive")
    deadline = _instant(payload.get("deadline"), "deadline")
    budget_manifest, inline_budget_hash = _budget_manifest(payload.get("budgetManifest", payload.get("budget_manifest")))
    provided_budget_hash = payload.get("budgetManifestSha256", payload.get("budget_manifest_sha256"))
    if provided_budget_hash not in (None, ""):
        provided_budget_hash = _hash(provided_budget_hash, "budget_manifest_sha256")
    if inline_budget_hash and provided_budget_hash not in (None, inline_budget_hash):
        raise RecoveryContractError("budget_manifest_hash_mismatch")
    budget_hash = provided_budget_hash or inline_budget_hash
    return RecoveryScope(
        campaign_id=campaign_id,
        recovery_id=recovery_id,
        plan_hash=supplied_hash,
        run_key=run_key,
        data_dates=data_dates,
        required_stages=required_stages,
        artifact_root=artifact_root,
        state_dir=state_dir,
        max_dates=max_dates,
        max_requests=max_requests,
        deadline=deadline,
        budget_manifest=budget_manifest,
        budget_manifest_sha256=budget_hash,
        contract_version=contract,
    )


def build_recovery_plan(
    *,
    campaign_id: str,
    recovery_id: str,
    run_key: str,
    data_dates: Sequence[str],
    required_stages: Sequence[str],
    artifact_root: str,
    state_dir: str,
    max_dates: int,
    max_requests: int,
    deadline: str,
    budget_manifest: Any,
    budget_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Create a canonical, immutable plan and its SHA-256 planHash."""

    draft: dict[str, Any] = {
        "schemaVersion": RECOVERY_SCHEMA_VERSION,
        "contractVersion": RECOVERY_CONTRACT_VERSION,
        "campaignId": campaign_id,
        "recoveryId": recovery_id,
        "runKey": run_key,
        "dataDates": list(data_dates),
        "requiredStages": list(required_stages),
        "artifactRoot": artifact_root,
        "stateDir": state_dir,
        "maxDates": max_dates,
        "maxRequests": max_requests,
        "deadline": deadline,
        "budgetManifest": budget_manifest,
    }
    if budget_manifest_sha256:
        draft["budgetManifestSha256"] = budget_manifest_sha256
    draft["planHash"] = canonical_sha256(draft)
    if len(_canonical_bytes(draft)) > MAX_PLAN_BYTES:
        raise RecoveryContractError("recovery_plan_size_exceeds_limit")
    # Round-trip through the validator so callers never receive an invalid
    # plan merely because they supplied a malformed argument.
    validate_recovery_plan(draft)
    return draft


def iter_execution_matrix(scope: RecoveryScope) -> tuple[dict[str, Any], ...]:
    """Return an explicit date-major execution matrix.

    No multi-date string is passed to an old collector interface.  Every
    matrix row owns one fixed dataDate and stage, so each output can carry an
    independent asOf and receipt.
    """

    rows: list[dict[str, Any]] = []
    for data_date in scope.data_dates:
        for stage in scope.required_stages:
            rows.append(
                {
                    "campaignId": scope.campaign_id,
                    "recoveryId": scope.recovery_id,
                    "planHash": scope.plan_hash,
                    "runKey": scope.run_key,
                    "dataDate": data_date,
                    "stage": stage,
                    "artifactRoot": f"{scope.artifact_root}/{data_date}/{stage}",
                    "stateDir": f"{scope.state_dir}/{data_date}/{stage}",
                    "deadline": scope.deadline,
                    "contractVersion": scope.contract_version,
                }
            )
    return tuple(rows)


def _runner_error_code(error: BaseException) -> str:
    if isinstance(error, RecoveryContractError):
        return error.code
    return "recovery_stage_runner_error"


def _runner_status(error_code: str) -> str:
    """Map a runner failure to a terminal evidence status.

    A source rate-limit/permission response must stop the batch rather than
    being retried against the same host.  Quality and unavailable-source
    outcomes are retained as evidence and are never presented as success.
    Everything else is retryable by the caller within the explicit attempt
    bound.
    """

    normalized = error_code.lower()
    if any(token in normalized for token in ("403", "429", "rate_limit", "rate_limited", "forbidden")):
        return "SOURCE_UNAVAILABLE"
    if any(token in normalized for token in ("quality", "lineage", "date_mismatch", "invalid_input", "invalid_receipt", "receipt_", "scope_", "plan_")):
        return "BLOCKED_QUALITY"
    if any(token in normalized for token in ("source_unavailable", "official_unavailable", "not_found")):
        return "SOURCE_UNAVAILABLE"
    return "FAILED_RETRYABLE"


def execute_recovery_plan(
    scope: RecoveryScope,
    *,
    attempt_count: int,
    existing_receipts: Iterable[Mapping[str, Any]] = (),
    stage_runner: Callable[[Mapping[str, Any], int], Mapping[str, Any]] | None = None,
    retry_attempts: int = 1,
    max_requests: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute a bounded date-major matrix through an injected stage runner.

    This is the shared offline orchestration contract used by the eventual
    production recovery runner.  The runner is deliberately injected: this
    module never calls an official source, D1, R2, GitHub or a workflow.  A
    missing runner therefore produces explicit ``PENDING`` records instead
    of manufacturing a successful receipt.  Existing receipts are reusable
    only when their scope, attempt and completion evidence match exactly.

    ``stage_runner(row, attempt_count)`` must return one complete receipt.  A
    runner exception is represented in the result and may be retried only
    within ``retry_attempts``.  The first source-rate-limit/permission failure
    stops the remaining matrix so the caller can persist a checkpoint without
    hammering an official host.
    """

    attempt_count = _nonnegative_int(attempt_count, "attempt_count")
    if retry_attempts < 1 or retry_attempts > 3:
        raise RecoveryContractError("invalid_retry_attempts")
    request_limit = scope.max_requests if max_requests is None else _nonnegative_int(max_requests, "max_requests")
    if request_limit <= 0 or request_limit > scope.max_requests:
        raise RecoveryContractError("max_requests_exceeds_scope")
    deadline = datetime.fromisoformat(scope.deadline.replace("Z", "+00:00"))
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    if observed_at > deadline:
        return {
            "status": "BLOCKED_QUALITY",
            "closed": False,
            "errorCode": "recovery_deadline_expired",
            "requestCount": 0,
            "records": [],
            "receipts": [],
            "completion": scope_completion(scope, []),
        }

    reusable: dict[tuple[str, str], Mapping[str, Any]] = {}
    validated_existing: list[Mapping[str, Any]] = []
    duplicate_keys: set[tuple[str, str]] = set()
    for raw in existing_receipts:
        try:
            candidate = validate_recovery_receipt(raw, scope=scope, expected_attempt=attempt_count)
        except RecoveryContractError:
            continue
        key = (candidate["dataDate"], candidate["stage"])
        if candidate["status"] != "COMPLETED":
            continue
        validated_existing.append(candidate)
        if key in reusable:
            duplicate_keys.add(key)
        else:
            reusable[key] = candidate

    records: list[dict[str, Any]] = []
    # Keep duplicate valid receipts in the completion input.  A duplicate is
    # an integrity failure, not merely a diagnostic; omitting it here would
    # allow a scope to close while the executor separately reports the
    # duplicate key.
    receipts: list[Mapping[str, Any]] = list(validated_existing)
    request_count = 0
    halted: str | None = None
    deadline_exceeded = False

    def current_time() -> datetime:
        # Tests may inject a fixed instant.  Production callers that omit
        # ``now`` get a fresh deadline check before every matrix row.
        value = observed_at if now is not None else datetime.now(timezone.utc)
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    for row in iter_execution_matrix(scope):
        key = (row["dataDate"], row["stage"])
        if key in duplicate_keys:
            # A duplicated logical slot is an integrity failure.  Do not
            # issue another source request that could hide the duplicate or
            # turn an immutable checkpoint into an apparently clean result.
            records.append(
                {
                    **row,
                    "status": "BLOCKED_QUALITY",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": "duplicate_receipt_key",
                }
            )
            continue
        if key in reusable and key not in duplicate_keys:
            records.append(
                {
                    **row,
                    "status": "COMPLETED",
                    "attemptCount": attempt_count,
                    "reused": True,
                    "requestCount": 0,
                }
            )
            continue
        if halted:
            records.append(
                {
                    **row,
                    "status": "SOURCE_UNAVAILABLE",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": halted,
                }
            )
            continue
        if deadline_exceeded or current_time() > deadline:
            deadline_exceeded = True
            records.append(
                {
                    **row,
                    "status": "BLOCKED_QUALITY",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": "recovery_deadline_expired",
                }
            )
            continue
        if stage_runner is None:
            records.append(
                {
                    **row,
                    "status": "FAILED_RETRYABLE",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": "recovery_stage_runner_not_configured",
                }
            )
            continue

        stage_record: dict[str, Any] | None = None
        for _attempt in range(retry_attempts):
            if deadline_exceeded or current_time() > deadline:
                stage_record = {
                    **row,
                    "status": "BLOCKED_QUALITY",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": "recovery_deadline_expired",
                }
                deadline_exceeded = True
                break
            if request_count >= request_limit:
                stage_record = {
                    **row,
                    "status": "FAILED_RETRYABLE",
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 0,
                    "errorCode": "recovery_request_budget_exceeded",
                }
                break
            request_count += 1
            try:
                produced = stage_runner(row, attempt_count)
                candidate = validate_recovery_receipt(
                    produced, scope=scope, expected_attempt=attempt_count
                )
                stage_record = {
                    **row,
                    "status": candidate["status"],
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 1,
                }
                receipts.append(candidate)
                if candidate["status"] != "COMPLETED":
                    stage_record["errorCode"] = "receipt_not_completed"
                break
            except Exception as error:  # runner boundary: keep evidence, never fake success
                code = _runner_error_code(error)
                status = _runner_status(code)
                stage_record = {
                    **row,
                    "status": status,
                    "attemptCount": attempt_count,
                    "reused": False,
                    "requestCount": 1,
                    "errorCode": code,
                }
                if status == "SOURCE_UNAVAILABLE":
                    halted = code
                    break
        assert stage_record is not None
        records.append(stage_record)

    completion = scope_completion(scope, receipts)
    failed = [record for record in records if record["status"] != "COMPLETED"]
    if completion["closed"]:
        status = "COMPLETED"
    elif halted or any(record["status"] == "SOURCE_UNAVAILABLE" for record in failed):
        status = "SOURCE_UNAVAILABLE"
    elif any(record["status"] == "BLOCKED_QUALITY" for record in failed):
        status = "BLOCKED_QUALITY"
    else:
        status = "FAILED_RETRYABLE"
    return {
        "status": status,
        "closed": completion["closed"],
        "requestCount": request_count,
        "records": records,
        "receipts": receipts,
        "completion": completion,
        "haltedErrorCode": halted,
        "duplicateReceiptKeys": [
            {"dataDate": data_date, "stage": stage}
            for data_date, stage in sorted(duplicate_keys)
        ],
    }


def artifact_reusable(
    existing: Mapping[str, Any] | None,
    *,
    input_sha256: str,
    contract_version: str = RECOVERY_CONTRACT_VERSION,
) -> bool:
    """Only reuse an output when its input hash and contract both match."""

    if not existing:
        return False
    try:
        return (
            _hash(existing.get("inputSha256", existing.get("input_sha256")), "input_sha256")
            == _hash(input_sha256, "input_sha256")
            and _string(existing.get("contractVersion", existing.get("contract_version")), "contract_version")
            == contract_version
            and existing.get("status") == "COMPLETED"
        )
    except RecoveryContractError:
        return False


def _receipt_key(value: Any) -> str:
    text = _string(value, "receipt_key")
    if not RECEIPT_KEY_RE.fullmatch(text):
        raise RecoveryContractError("invalid_receipt_key")
    return text


def _input_objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RecoveryContractError("invalid_input_objects")
    result: list[dict[str, Any]] = []
    for item in value:
        row = _require_mapping(item, "invalid_input_object")
        key = _string(row.get("key"), "input_object_key", pattern=IDENTIFIER_RE)
        sha = _hash(row.get("sha256"), "input_object_sha256")
        source_dates_raw = row.get("sourceDates", row.get("source_dates", []))
        if not isinstance(source_dates_raw, list) or not source_dates_raw:
            raise RecoveryContractError("invalid_input_source_dates")
        source_dates = [_date(item_date, "source_date") for item_date in source_dates_raw]
        fingerprint = row.get("sourceFingerprint", row.get("source_fingerprint"))
        if fingerprint is None or not isinstance(fingerprint, str) or not fingerprint.strip():
            raise RecoveryContractError("invalid_input_source_fingerprint")
        result.append(
            {
                "key": key,
                "sha256": sha,
                "sourceDates": source_dates,
                "sourceFingerprint": fingerprint.strip(),
            }
        )
    return result


def _versions(value: Any) -> dict[str, str]:
    row = _require_mapping(value, "invalid_versions")
    result: dict[str, str] = {}
    for field in ("schema", "calculation", "classification"):
        result[field] = _string(row.get(field), f"{field}_version", pattern=IDENTIFIER_RE)
    return result


def _qa(value: Any) -> dict[str, Any]:
    row = _require_mapping(value, "invalid_qa")
    result = str(row.get("result") or "").upper()
    if result not in {"PASS", "FAIL", "BLOCKED", "UNKNOWN"}:
        raise RecoveryContractError("invalid_qa_result")
    evidence = row.get("evidenceKeys", row.get("evidence_keys", []))
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise RecoveryContractError("invalid_qa_evidence_keys")
    if result == "PASS" and not evidence:
        raise RecoveryContractError("qa_pass_requires_evidence")
    return {"result": result, "evidenceKeys": [item.strip() for item in evidence]}


def _output(value: Any, *, status: str) -> dict[str, Any]:
    row = _require_mapping(value, "invalid_output")
    result: dict[str, Any] = {
        "manifestSha256": _hash(row.get("manifestSha256", row.get("manifest_sha256")), "manifest_sha256"),
        "releaseId": _string(row.get("releaseId", row.get("release_id")), "release_id", pattern=IDENTIFIER_RE),
        "slot": _string(row.get("slot"), "slot", pattern=IDENTIFIER_RE),
        "asOf": _date(row.get("asOf", row.get("as_of")), "output_as_of"),
    }
    if status == "COMPLETED":
        for field in result.values():
            if field in (None, ""):
                raise RecoveryContractError("completed_receipt_output_missing")
    return result


def _historical(value: Any) -> dict[str, Any]:
    row = _require_mapping(value, "invalid_historical_verification")
    readable = row.get("dateReadable", row.get("date_readable"))
    if not isinstance(readable, bool):
        raise RecoveryContractError("invalid_historical_date_readable")
    active_before = row.get("activeBefore", row.get("active_before"))
    active_after = row.get("activeAfter", row.get("active_after"))
    if active_before is not None and not isinstance(active_before, str):
        raise RecoveryContractError("invalid_active_before")
    if active_after is not None and not isinstance(active_after, str):
        raise RecoveryContractError("invalid_active_after")
    return {"dateReadable": readable, "activeBefore": active_before, "activeAfter": active_after}


def _resources(value: Any) -> dict[str, Any]:
    row = _require_mapping(value, "invalid_resource_metrics")
    result: dict[str, Any] = {}
    for field in ("d1RowsRead", "d1RowsWritten", "workerRequests", "r2ClassA", "r2ClassB", "r2Bytes", "actionsMinutes"):
        result[field] = _nonnegative_int(row.get(field), field)
    result["measuredAt"] = _instant(row.get("measuredAt", row.get("measured_at")), "measured_at")
    return result


def validate_recovery_receipt(
    value: Mapping[str, Any],
    *,
    scope: RecoveryScope | None = None,
    expected_attempt: int | None = None,
) -> dict[str, Any]:
    """Validate a receipt and return a canonical camelCase representation."""

    payload = _require_mapping(value, "invalid_recovery_receipt")
    if payload.get("schemaVersion") != RECOVERY_SCHEMA_VERSION:
        raise RecoveryContractError("unknown_receipt_schema_version")
    campaign_id = _string(payload.get("campaignId"), "campaign_id", pattern=IDENTIFIER_RE)
    recovery_id = _string(payload.get("recoveryId"), "recovery_id", pattern=IDENTIFIER_RE)
    plan_hash = _hash(payload.get("planHash"), "plan_hash")
    run_key = _string(payload.get("runKey"), "run_key")
    parsed_run = parse_run_key(run_key)
    attempt = _nonnegative_int(payload.get("attemptCount"), "attempt_count")
    if expected_attempt is not None and attempt != expected_attempt:
        raise RecoveryContractError("receipt_attempt_mismatch")
    data_date = _date(payload.get("dataDate"), "data_date")
    market = _string(payload.get("market"), "market", pattern=IDENTIFIER_RE)
    stage = _string(payload.get("stage"), "stage", pattern=IDENTIFIER_RE)
    if stage not in RECOVERY_STAGE_SET:
        raise RecoveryContractError("unknown_receipt_stage")
    status = str(payload.get("status") or "").upper()
    if status not in RECOVERY_STATUS_SET:
        raise RecoveryContractError("invalid_receipt_status")
    inputs = _input_objects(payload.get("inputObjects", payload.get("input_objects")))
    versions = _versions(payload.get("versions"))
    producer_sha = _hash(payload.get("producerBuildSha", payload.get("producer_build_sha")), "producer_build_sha")
    collector_run_id = _string(payload.get("collectorRunId", payload.get("collector_run_id")), "collector_run_id", pattern=IDENTIFIER_RE)
    publish_run_id = payload.get("publishRunId", payload.get("publish_run_id"))
    if publish_run_id is not None:
        publish_run_id = _string(publish_run_id, "publish_run_id", pattern=IDENTIFIER_RE)
    output = _output(payload.get("output"), status=status) if payload.get("output") is not None else None
    qa = _qa(payload.get("qa"))
    historical = _historical(payload.get("historical"))
    resources = _resources(payload.get("resources"))
    completed_at = payload.get("completedAt", payload.get("completed_at"))
    completed_at = _instant(completed_at, "completed_at") if completed_at is not None else None
    if status == "COMPLETED":
        if qa["result"] != "PASS" or output is None or not historical["dateReadable"]:
            raise RecoveryContractError("completed_receipt_evidence_incomplete")
        if output["asOf"] != data_date:
            raise RecoveryContractError("receipt_output_date_mismatch")
        if completed_at is None:
            raise RecoveryContractError("completed_at_required")
    elif completed_at is not None:
        raise RecoveryContractError("nonterminal_receipt_completed_at")
    # A multi-date recovery may deliberately use one registered window run
    # key for its approved batch.  The plan, rather than the runner's current
    # date, is the authority for each receipt's dataDate.
    if scope is not None:
        if campaign_id != scope.campaign_id or recovery_id != scope.recovery_id:
            raise RecoveryContractError("receipt_scope_identity_mismatch")
        if plan_hash != scope.plan_hash or run_key != scope.run_key:
            raise RecoveryContractError("receipt_scope_binding_mismatch")
        if data_date not in scope.data_dates:
            raise RecoveryContractError("receipt_date_outside_scope")
        if stage not in scope.required_stages:
            raise RecoveryContractError("receipt_stage_outside_scope")
    return {
        "schemaVersion": RECOVERY_SCHEMA_VERSION,
        "campaignId": campaign_id,
        "recoveryId": recovery_id,
        "planHash": plan_hash,
        "runKey": run_key,
        "attemptCount": attempt,
        "dataDate": data_date,
        "market": market,
        "stage": stage,
        "status": status,
        "inputObjects": inputs,
        "versions": versions,
        "producerBuildSha": producer_sha,
        "collectorRunId": collector_run_id,
        "publishRunId": publish_run_id,
        "output": output,
        "qa": qa,
        "historical": historical,
        "resources": resources,
        "completedAt": completed_at,
    }


def receipt_bytes(value: Mapping[str, Any]) -> tuple[bytes, str]:
    """Validate and serialize a receipt without adding a mutable timestamp."""

    validated = validate_recovery_receipt(value)
    encoded = _canonical_bytes(validated) + b"\n"
    return encoded, sha256_bytes(encoded)


def receipt_key(*, recovery_id: str, data_date: str, stage: str, attempt: int) -> str:
    recovery_id = _string(recovery_id, "recovery_id", pattern=IDENTIFIER_RE)
    data_date = _date(data_date, "data_date")
    stage = _string(stage, "stage", pattern=IDENTIFIER_RE)
    if stage not in RECOVERY_STAGE_SET:
        raise RecoveryContractError("unknown_receipt_stage")
    attempt = _nonnegative_int(attempt, "attempt_count")
    key = f"market-data/recovery/receipts/{recovery_id}/{data_date}/{stage}/{attempt}.json"
    _receipt_key(key)
    return key


def scope_completion(scope: RecoveryScope, receipts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Close a scope only when every required date/stage has a valid receipt."""

    expected = {(data_date, stage) for data_date in scope.data_dates for stage in scope.required_stages}
    valid: dict[tuple[str, str], dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = []
    duplicates: list[dict[str, str]] = []
    for raw in receipts:
        try:
            receipt = validate_recovery_receipt(raw, scope=scope)
            key = (receipt["dataDate"], receipt["stage"])
            if receipt["status"] != "COMPLETED":
                invalid.append({"reason": "not_completed", "dataDate": key[0], "stage": key[1]})
            elif key in valid:
                duplicates.append({"dataDate": key[0], "stage": key[1]})
            else:
                valid[key] = receipt
        except RecoveryContractError as exc:
            invalid.append({"reason": exc.code})
    missing = [
        {"dataDate": data_date, "stage": stage}
        for data_date, stage in sorted(expected - set(valid))
    ]
    closed = not missing and not invalid and not duplicates
    return {
        "status": "COMPLETED" if closed else "INCOMPLETE",
        "closed": closed,
        "expectedCount": len(expected),
        "validCount": len(valid),
        "missing": missing,
        "invalid": invalid,
        "duplicates": duplicates,
    }


@dataclass(frozen=True)
class RecoveryBudgetLimits:
    d1_reads: int
    d1_writes: int
    workers_requests: int
    actions_minutes: int
    r2_bytes: int
    r2_class_a: int | None
    r2_class_b: int | None


@dataclass(frozen=True)
class RecoveryBudgetEstimate:
    d1_reads: int = 0
    d1_writes: int = 0
    workers_requests: int = 0
    actions_minutes: int = 0
    r2_bytes: int = 0
    r2_class_a: int = 0
    r2_class_b: int = 0

    def plus(self, other: "RecoveryBudgetEstimate") -> "RecoveryBudgetEstimate":
        return RecoveryBudgetEstimate(
            d1_reads=self.d1_reads + other.d1_reads,
            d1_writes=self.d1_writes + other.d1_writes,
            workers_requests=self.workers_requests + other.workers_requests,
            actions_minutes=self.actions_minutes + other.actions_minutes,
            r2_bytes=self.r2_bytes + other.r2_bytes,
            r2_class_a=self.r2_class_a + other.r2_class_a,
            r2_class_b=self.r2_class_b + other.r2_class_b,
        )


def _limits_from_config(config: Mapping[str, Any]) -> RecoveryBudgetLimits:
    limits = _require_mapping(config.get("limits", {}), "invalid_budget_config")
    hard = _require_mapping(config.get("hardLimits", {}), "invalid_budget_config")
    storage = _require_mapping(config.get("storage", {}), "invalid_budget_config")

    def pick(name: str, fallback: str | None = None) -> int:
        value = limits.get(name)
        if value is None and fallback:
            value = hard.get(fallback)
        if value is None:
            raise RecoveryContractError(f"budget_limit_missing:{name}")
        value = _nonnegative_int(value, name)
        hard_value = hard.get(fallback or name)
        if hard_value is not None:
            hard_value = _nonnegative_int(hard_value, f"hard_{name}")
            # The internal hard stop is never allowed to exceed the provider
            # limit.  A budget report therefore gates on the stricter bound.
            value = min(value, hard_value)
        return value

    def pick_optional(name: str) -> int | None:
        value = config.get(name)
        if value is None:
            value = limits.get(name)
        if value is None:
            return None
        return _nonnegative_int(value, name)

    return RecoveryBudgetLimits(
        d1_reads=pick("d1RowsReadPerDay", "d1RowsReadPerDay"),
        d1_writes=pick("d1RowsWrittenPerDay", "d1RowsWrittenPerDay"),
        workers_requests=pick("workersRequestsPerDay", "workersRequestsPerDay"),
        actions_minutes=pick("githubActionsMinutesPerMonth", "githubActionsMinutesPerMonth"),
        r2_bytes=_nonnegative_int(storage.get("r2SoftBytes", 0), "r2SoftBytes"),
        # R2 operation limits are account/package dependent.  A missing
        # value is explicitly unknown and must block a remote recovery.
        r2_class_a=pick_optional("r2ClassA"),
        r2_class_b=pick_optional("r2ClassB"),
    )


def _usage(value: Mapping[str, Any] | None, field: str) -> int | None:
    if value is None or field not in value:
        return None
    return _nonnegative_int(value[field], field)


def evaluate_recovery_budget(
    *,
    config: Mapping[str, Any],
    estimate: RecoveryBudgetEstimate,
    measured: Mapping[str, Any] | None = None,
    reserved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a conservative upper bound before any remote request.

    Account usage and reservations are intentionally separate from the stage
    estimate.  Missing account metrics are ``unknown`` rather than zero; a
    remote caller must stop until they are supplied.
    """

    limits = _limits_from_config(config)
    measured = measured or {}
    reserved = reserved or {}
    fields = (
        ("d1Reads", "d1_reads"),
        ("d1Writes", "d1_writes"),
        ("workersRequests", "workers_requests"),
        ("actionsMinutes", "actions_minutes"),
        ("r2Bytes", "r2_bytes"),
        ("r2ClassA", "r2_class_a"),
        ("r2ClassB", "r2_class_b"),
    )
    unknown: list[str] = []
    projected: dict[str, int] = {}
    for public, attr in fields:
        current = _usage(measured, public)
        reserve = _usage(reserved, public)
        if current is None or reserve is None:
            unknown.append(public)
            continue
        projected[public] = current + reserve + int(getattr(estimate, attr))
    limit_values = {
        "d1Reads": limits.d1_reads,
        "d1Writes": limits.d1_writes,
        "workersRequests": limits.workers_requests,
        "actionsMinutes": limits.actions_minutes,
        "r2Bytes": limits.r2_bytes,
        "r2ClassA": limits.r2_class_a,
        "r2ClassB": limits.r2_class_b,
    }
    unknown_limits = [key for key, value in limit_values.items() if value is None]
    unknown.extend(f"{key}Limit" for key in unknown_limits)
    over = {
        key: value
        for key, value in projected.items()
        if limit_values[key] is not None and value > limit_values[key]
    }
    if unknown:
        decision = "unknown_metrics"
        reason = "account usage or reserved capacity is missing; fail closed before remote recovery"
    elif over:
        decision = "over_budget"
        reason = "projected recovery upper bound exceeds an account/internal limit"
    else:
        decision = "allow"
        reason = "projected recovery upper bound is within the supplied limits"
    return {
        "schemaVersion": 1,
        "decision": decision,
        "reason": reason,
        "unknownMetrics": unknown,
        "overBudget": over,
        "estimate": {
            "d1Reads": estimate.d1_reads,
            "d1Writes": estimate.d1_writes,
            "workersRequests": estimate.workers_requests,
            "actionsMinutes": estimate.actions_minutes,
            "r2Bytes": estimate.r2_bytes,
            "r2ClassA": estimate.r2_class_a,
            "r2ClassB": estimate.r2_class_b,
        },
        "projected": projected,
        "limits": limit_values,
    }


def estimate_recovery_budget(
    *,
    scope: RecoveryScope,
    config: Mapping[str, Any],
    retry_attempts: int = 3,
    measured: Mapping[str, Any] | None = None,
    reserved: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate all required stages, retries and physical write overhead."""

    if retry_attempts < 1 or retry_attempts > 3:
        raise RecoveryContractError("invalid_retry_attempts")
    stage_budgets = _require_mapping(config.get("stageBudgets", {}), "invalid_stage_budgets")
    estimate = RecoveryBudgetEstimate()
    for _data_date in scope.data_dates:
        for stage in scope.required_stages:
            raw = stage_budgets.get(stage)
            if raw is None:
                # Slot names intentionally map to the checked-in budget names
                # where possible.  A missing budget is not silently treated as
                # zero; it is reported as unknown by the caller.
                aliases = {"dashboard": "publish", "etf_penetration": "publish", "stock_flow": "coreCollection", "pcf_exact": "pcfExact", "core_collection": "coreCollection"}
                raw = stage_budgets.get(aliases.get(stage, stage))
            if not isinstance(raw, Mapping):
                raise RecoveryContractError(f"stage_budget_missing:{stage}")
            one = RecoveryBudgetEstimate(
                d1_reads=_nonnegative_int(raw.get("rowsRead", raw.get("d1RowsRead", 0)), f"{stage}_rows_read"),
                d1_writes=_nonnegative_int(raw.get("rowsWritten", raw.get("d1RowsWritten", 0)), f"{stage}_rows_written"),
                workers_requests=_nonnegative_int(raw.get("workerRequests", 1), f"{stage}_worker_requests"),
                actions_minutes=_nonnegative_int(raw.get("actionsMinutes", 20), f"{stage}_actions_minutes"),
                r2_bytes=_nonnegative_int(raw.get("r2Bytes", 0), f"{stage}_r2_bytes"),
                r2_class_a=_nonnegative_int(raw.get("r2ClassA", 1), f"{stage}_r2_class_a"),
                r2_class_b=_nonnegative_int(raw.get("r2ClassB", 1), f"{stage}_r2_class_b"),
            )
            estimate = estimate.plus(
                RecoveryBudgetEstimate(
                    d1_reads=one.d1_reads * retry_attempts,
                    d1_writes=one.d1_writes * retry_attempts,
                    workers_requests=one.workers_requests * retry_attempts,
                    actions_minutes=one.actions_minutes * retry_attempts,
                    r2_bytes=one.r2_bytes * retry_attempts,
                    r2_class_a=one.r2_class_a * retry_attempts,
                    r2_class_b=one.r2_class_b * retry_attempts,
                )
            )
    report = evaluate_recovery_budget(config=config, estimate=estimate, measured=measured, reserved=reserved)
    report["scope"] = {
        "campaignId": scope.campaign_id,
        "recoveryId": scope.recovery_id,
        "planHash": scope.plan_hash,
        "dataDates": list(scope.data_dates),
        "requiredStages": list(scope.required_stages),
        "retryAttempts": retry_attempts,
    }
    return report


__all__ = [
    "RECOVERY_CONTRACT_VERSION",
    "RECOVERY_SCHEMA_VERSION",
    "RECOVERY_STAGES",
    "RecoveryBudgetEstimate",
    "RecoveryContractError",
    "RecoveryScope",
    "artifact_reusable",
    "assert_recovery_output_path",
    "build_recovery_plan",
    "canonical_sha256",
    "estimate_recovery_budget",
    "execute_recovery_plan",
    "evaluate_recovery_budget",
    "iter_execution_matrix",
    "parse_run_key",
    "plan_bytes",
    "receipt_bytes",
    "receipt_key",
    "scope_completion",
    "sha256_bytes",
    "validate_recovery_plan",
    "validate_recovery_receipt",
]
