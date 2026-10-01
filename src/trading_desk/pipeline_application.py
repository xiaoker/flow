"""Shared, side-effect-free application contracts for the market pipeline.

The daily collector, read-model publisher and bounded recovery runner still
own their transport and source-specific work.  This module is the small
common seam between them: it validates the execution identity, creates
deterministic stage plans/checkpoints, and provides the bounded retry policy.
It deliberately performs no filesystem, D1, R2, GitHub or official-source
operation.

The contracts are intentionally additive in v0.6-A.  Existing callers may
continue to emit their legacy manifests/receipts; when a caller opts into the
application metadata, the metadata is derived from the same inputs and does
not change the existing data or release decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Sequence


APPLICATION_SCHEMA_VERSION = 1
APPLICATION_CONTRACT_VERSION = "pipeline-application-v1"
APPLICATION_STAGES = (
    "core_collection",
    "normalize",
    "build",
    "verify",
    "publish",
    "verify_visible",
)
APPLICATION_STATUSES = ("COMPLETED", "FAILED_RETRYABLE", "BLOCKED_QUALITY", "PENDING")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_KEY_RE = re.compile(
    r"^daily-market-data:(?P<date>\d{4}-\d{2}-\d{2}):(?P<window>[a-z_]+):(?P<version>v\d+)$"
)


class ApplicationContractError(ValueError):
    """Raised when a shared identity or checkpoint violates its contract."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ApplicationContractError(f"invalid_{field}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ApplicationContractError(f"invalid_{field}") from exc
    return value


def _sha(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ApplicationContractError(f"invalid_{field}")
    return value


def parse_application_run_key(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise ApplicationContractError("invalid_run_key")
    match = _RUN_KEY_RE.fullmatch(value)
    if not match:
        raise ApplicationContractError("invalid_run_key")
    parsed = _date(match.group("date"), "run_key_date")
    version = match.group("version")
    if version != "v1":
        raise ApplicationContractError("unknown_run_key_version")
    if match.group("window") not in {"evening", "morning", "morning_exact", "reconcile"}:
        raise ApplicationContractError("unknown_run_key_window")
    return {"date": parsed, "window": match.group("window"), "version": version}


@dataclass(frozen=True)
class ApplicationIdentity:
    """The stable identity shared by daily, resume and recovery execution."""

    run_key: str
    window_date: str
    target_date: str | None
    contract_version: str = APPLICATION_CONTRACT_VERSION
    producer_build_sha: str | None = None

    def __post_init__(self) -> None:
        parsed = parse_application_run_key(self.run_key)
        if parsed["date"] != self.window_date:
            raise ApplicationContractError("run_key_window_date_mismatch")
        _date(self.window_date, "window_date")
        if self.target_date is not None:
            _date(self.target_date, "target_date")
        if self.contract_version != APPLICATION_CONTRACT_VERSION:
            raise ApplicationContractError("unknown_application_contract_version")
        _sha(self.producer_build_sha, "producer_build_sha", allow_none=True)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "runKey": self.run_key,
            "windowDate": self.window_date,
            "targetDate": self.target_date,
            "contractVersion": self.contract_version,
        }
        if self.producer_build_sha is not None:
            value["producerBuildSha"] = self.producer_build_sha
        return value


def build_application_identity(
    *,
    run_key: str,
    window_date: str,
    target_date: str | None = None,
    producer_build_sha: str | None = None,
) -> ApplicationIdentity:
    """Construct one validated identity at every application boundary."""

    return ApplicationIdentity(
        run_key=run_key,
        window_date=window_date,
        target_date=target_date,
        producer_build_sha=producer_build_sha,
    )


def build_application_plan(
    identity: ApplicationIdentity,
    stages: Sequence[str],
    *,
    stage_inputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a stable, side-effect-free stage plan.

    ``stage_inputs`` is optional during the extraction phase.  When present,
    every digest is checked and carried into the plan so a resume can prove
    that only unchanged stages are reusable.
    """

    if not stages or len(set(stages)) != len(stages):
        raise ApplicationContractError("invalid_application_stages")
    unknown = set(stages) - set(APPLICATION_STAGES)
    if unknown:
        raise ApplicationContractError("unknown_application_stage", ",".join(sorted(unknown)))
    inputs: dict[str, str] = {}
    for stage in stages:
        if stage_inputs is not None:
            digest = _sha(stage_inputs.get(stage), f"{stage}_input_sha256")
            assert digest is not None
            inputs[stage] = digest
    payload: dict[str, Any] = {
        "schemaVersion": APPLICATION_SCHEMA_VERSION,
        "contractVersion": identity.contract_version,
        "identity": identity.as_dict(),
        "stages": list(stages),
    }
    if inputs:
        payload["stageInputs"] = inputs
    payload["planHash"] = canonical_sha256(payload)
    return payload


def build_checkpoint(
    *,
    identity: ApplicationIdentity,
    stage: str,
    status: str,
    input_sha256: str,
    output_sha256: str | None = None,
    allowed_side_effects: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a deterministic checkpoint without writing it anywhere."""

    if stage not in APPLICATION_STAGES and not re.fullmatch(r"[a-z_]{1,64}", stage):
        raise ApplicationContractError("invalid_application_stage")
    if status not in APPLICATION_STATUSES:
        raise ApplicationContractError("invalid_application_status")
    input_hash = _sha(input_sha256, "input_sha256")
    output_hash = _sha(output_sha256, "output_sha256", allow_none=True)
    if status == "COMPLETED" and output_hash is None:
        raise ApplicationContractError("completed_checkpoint_output_missing")
    effects = sorted({str(item) for item in allowed_side_effects if str(item).strip()})
    if any(not re.fullmatch(r"[a-z0-9_.:/-]{1,120}", item) for item in effects):
        raise ApplicationContractError("invalid_allowed_side_effect")
    payload: dict[str, Any] = {
        "schemaVersion": APPLICATION_SCHEMA_VERSION,
        "contractVersion": identity.contract_version,
        "identity": identity.as_dict(),
        "stage": stage,
        "status": status,
        "inputSha256": input_hash,
        "outputSha256": output_hash,
        "allowedSideEffects": effects,
        "evidence": dict(evidence or {}),
    }
    payload["checkpointSha256"] = canonical_sha256(payload)
    return payload


def checkpoint_is_reusable(
    value: Mapping[str, Any] | None,
    *,
    identity: ApplicationIdentity | None = None,
    stage: str | None = None,
    input_sha256: str,
    contract_version: str = APPLICATION_CONTRACT_VERSION,
) -> bool:
    """Return true only for an exact, completed checkpoint.

    Legacy records without ``checkpointSha256`` remain readable during the
    extraction phase, but any new application checkpoint must carry and
    validate its self-hash.  This lets old wrappers continue unchanged while
    preventing a merely existing file from being treated as completed.
    """

    if not isinstance(value, Mapping) or value.get("status") != "COMPLETED":
        return False
    try:
        if value.get("contractVersion") != contract_version:
            return False
        if value.get("inputSha256") != _sha(input_sha256, "input_sha256"):
            return False
        if stage is not None and value.get("stage") != stage:
            return False
        if identity is not None and value.get("identity") != identity.as_dict():
            return False
        output = value.get("outputSha256")
        if output is None:
            # Compatibility for the pre-v0.6 artifact_reusable contract.
            return "checkpointSha256" not in value
        _sha(output, "output_sha256")
        supplied = value.get("checkpointSha256")
        if supplied is None:
            return False
        return supplied == canonical_sha256({key: item for key, item in value.items() if key != "checkpointSha256"})
    except ApplicationContractError:
        return False


def execute_application_plan(
    *,
    identity: ApplicationIdentity,
    stages: Sequence[str],
    stage_inputs: Mapping[str, str],
    stage_runner: Callable[[str, str], Mapping[str, Any]],
    existing_checkpoints: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Execute an injected stage function through one common checkpoint seam.

    This is deliberately an in-process executor: it never performs I/O by
    itself.  The collector, publisher and recovery adapters inject their
    existing operations, while the executor owns stage ordering, exact input
    identity and reuse decisions.  The returned records are suitable for a
    caller's existing manifest/receipt format and contain no credentials.
    """

    plan = build_application_plan(identity, stages, stage_inputs=stage_inputs)
    known: dict[str, Mapping[str, Any]] = {}
    for candidate in existing_checkpoints:
        if not isinstance(candidate, Mapping):
            continue
        stage = candidate.get("stage")
        if isinstance(stage, str) and stage not in known:
            known[stage] = candidate
    checkpoints: list[Mapping[str, Any]] = []
    records: list[dict[str, Any]] = []
    for stage in stages:
        input_sha = stage_inputs[stage]
        previous = known.get(stage)
        if checkpoint_is_reusable(
            previous,
            identity=identity,
            stage=stage,
            input_sha256=input_sha,
        ):
            checkpoint = previous
            reused = True
        else:
            checkpoint = stage_runner(stage, input_sha)
            if not isinstance(checkpoint, Mapping):
                raise ApplicationContractError("stage_runner_checkpoint_required")
            if checkpoint.get("identity") != identity.as_dict() or checkpoint.get("stage") != stage:
                raise ApplicationContractError("stage_checkpoint_identity_mismatch")
            if checkpoint.get("inputSha256") != input_sha:
                raise ApplicationContractError("stage_checkpoint_input_mismatch")
            if checkpoint.get("status") not in APPLICATION_STATUSES:
                raise ApplicationContractError("invalid_stage_checkpoint_status")
            if checkpoint.get("status") == "COMPLETED" and not checkpoint_is_reusable(
                checkpoint,
                identity=identity,
                stage=stage,
                input_sha256=input_sha,
            ):
                raise ApplicationContractError("invalid_stage_checkpoint_hash")
            reused = False
        checkpoints.append(checkpoint)
        records.append(
            {
                "stage": stage,
                "status": checkpoint.get("status"),
                "reused": reused,
                "checkpointSha256": checkpoint.get("checkpointSha256"),
            }
        )
    statuses = [str(item.get("status")) for item in checkpoints]
    if all(status == "COMPLETED" for status in statuses):
        status = "COMPLETED"
    elif any(status == "BLOCKED_QUALITY" for status in statuses):
        status = "BLOCKED_QUALITY"
    elif any(status == "FAILED_RETRYABLE" for status in statuses):
        status = "FAILED_RETRYABLE"
    else:
        status = "PENDING"
    return {
        "schemaVersion": APPLICATION_SCHEMA_VERSION,
        "contractVersion": identity.contract_version,
        "identity": identity.as_dict(),
        "planHash": plan["planHash"],
        "status": status,
        "records": records,
        "checkpoints": list(checkpoints),
    }


def retry_bounded(
    operation: Callable[[], Any],
    *,
    max_attempts: int,
    should_retry_exception: Callable[[BaseException], bool] | None = None,
    should_retry_result: Callable[[Any], bool] | None = None,
    before_attempt: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run one bounded retry loop used by all application transports.

    The caller still owns HTTP response classification and side effects.  The
    helper only controls attempt count/backoff and re-raises the original
    final error, preserving existing error messages and fail-closed behavior.
    """

    if not isinstance(max_attempts, int) or max_attempts <= 0 or max_attempts > 8:
        raise ApplicationContractError("invalid_retry_attempts")
    retry_exception = should_retry_exception or (lambda _exc: True)
    retry_result = should_retry_result or (lambda _result: False)
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        if before_attempt is not None:
            before_attempt(attempt)
        try:
            result = operation()
            if not retry_result(result):
                return result
            last_error = RuntimeError("retryable operation result")
        except Exception as exc:
            if not retry_exception(exc):
                raise
            last_error = exc
        if attempt < max_attempts - 1:
            sleep(2**attempt)
    if last_error is not None:
        raise last_error
    raise ApplicationContractError("retry_exhausted_without_result")


__all__ = [
    "APPLICATION_CONTRACT_VERSION",
    "APPLICATION_SCHEMA_VERSION",
    "APPLICATION_STAGES",
    "ApplicationContractError",
    "ApplicationIdentity",
    "build_application_identity",
    "build_application_plan",
    "build_checkpoint",
    "canonical_sha256",
    "checkpoint_is_reusable",
    "execute_application_plan",
    "parse_application_run_key",
    "retry_bounded",
]
