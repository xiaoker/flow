"""Candidate cross-environment entry for the shared pipeline executor.

This module is intentionally a thin adapter around
``pipeline_application.execute_application_plan``.  It validates the
execution envelope that a local operator and a GitHub workflow must use, then
feeds the same redacted stage fixture into the same application executor.
It does not contact D1, R2, Cloudflare, GitHub, an official source, or an
active-release pointer.  The candidate entry is therefore safe to exercise
before a production runner cutover is authorized.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .pipeline_application import (
    APPLICATION_CONTRACT_VERSION,
    APPLICATION_STAGES,
    ApplicationContractError,
    ApplicationIdentity,
    build_application_identity,
    build_checkpoint,
    canonical_sha256,
    execute_application_plan,
)


EXECUTOR_SCHEMA_VERSION = 1
EXECUTOR_CONTRACT_VERSION = "pipeline-executor-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MODES = {"ordinary", "recovery"}
_AUTH_KINDS = {"existing-admin", "approved-recovery"}
_PROTECTED_PATHS = {"memory"}


class PipelineRunnerError(ValueError):
    """Raised when the candidate execution envelope is unsafe or incomplete."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PipelineRunnerError(f"invalid_{field}")
    return value


def _producer_sha(value: Any) -> str:
    """Validate a Git object ID, distinct from SHA-256 artifact digests."""

    if not isinstance(value, str) or not _GIT_OBJECT_ID_RE.fullmatch(value):
        raise PipelineRunnerError("invalid_producer_build_sha")
    return value


def _date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PipelineRunnerError(f"invalid_{field}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PipelineRunnerError(f"invalid_{field}") from exc
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PipelineRunnerError(f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineRunnerError(f"invalid_{field}") from exc
    if parsed.tzinfo is None:
        raise PipelineRunnerError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PipelineRunnerError(f"{field}_object_required")
    return value


def _safe_json(value: Any) -> Any:
    """Return JSON-compatible redacted data, never credentials."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
            if str(key).lower() not in {"token", "secret", "password", "authorization"}
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def dependency_lock_digest(paths: Sequence[str | Path], *, root: Path) -> dict[str, Any]:
    """Hash exact lock files in stable order for local/CI identity binding."""

    if not paths:
        raise PipelineRunnerError("dependency_lock_required")
    entries: list[dict[str, Any]] = []
    root = root.resolve()
    for raw in paths:
        path = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise PipelineRunnerError("dependency_lock_path_escape") from exc
        if not path.is_file():
            raise PipelineRunnerError("dependency_lock_missing", relative)
        content = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    entries.sort(key=lambda item: item["path"])
    return {
        "files": entries,
        "sha256": canonical_sha256(entries),
    }


def _assert_safe_output(path: Path) -> None:
    parts = path.resolve().parts
    if any(part in _PROTECTED_PATHS for part in parts):
        raise PipelineRunnerError("protected_output_path")
    for index, part in enumerate(parts[:-1]):
        if part == "reports" and parts[index + 1] == "phase0" and path.name.startswith("latest-"):
            raise PipelineRunnerError("protected_output_path")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _assert_safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    temporary.replace(path)


@dataclass(frozen=True)
class PipelineExecutionContext:
    identity: ApplicationIdentity
    mode: str
    lease_owner: str
    lease_expires_at: str
    authorization_kind: str
    recovery_id: str | None
    recovery_plan_sha256: str | None
    dependency_lock_sha256: str

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": EXECUTOR_SCHEMA_VERSION,
            "contractVersion": EXECUTOR_CONTRACT_VERSION,
            "identity": self.identity.as_dict(),
            "mode": self.mode,
            "lease": {
                "owner": self.lease_owner,
                "expiresAt": self.lease_expires_at,
            },
            "authorization": {"kind": self.authorization_kind, "verified": True},
            "dependencyLockSha256": self.dependency_lock_sha256,
        }
        if self.recovery_id is not None:
            result["recovery"] = {
                "recoveryId": self.recovery_id,
                "planSha256": self.recovery_plan_sha256,
            }
        return result


def build_execution_context(
    payload: Mapping[str, Any],
    *,
    producer_build_sha: str,
    dependency_lock_sha256: str,
    now: datetime | None = None,
) -> PipelineExecutionContext:
    """Validate auth, lease, recovery approval and exact build identity."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    identity_payload = _mapping(payload.get("identity", payload), "identity")
    run_key = identity_payload.get("runKey")
    window_date = identity_payload.get("windowDate")
    target_date = identity_payload.get("targetDate")
    if target_date is not None:
        _date(target_date, "target_date")
    if identity_payload.get("contractVersion") not in {None, APPLICATION_CONTRACT_VERSION}:
        raise PipelineRunnerError("unknown_application_contract_version")
    producer_build_sha = _producer_sha(producer_build_sha)
    declared_sha = identity_payload.get("producerBuildSha")
    if declared_sha is not None and declared_sha != producer_build_sha:
        raise PipelineRunnerError("producer_build_sha_mismatch")
    try:
        identity = build_application_identity(
            run_key=run_key,
            window_date=window_date,
            target_date=target_date,
            producer_build_sha=producer_build_sha,
        )
    except (ApplicationContractError, TypeError) as exc:
        if isinstance(exc, ApplicationContractError):
            raise PipelineRunnerError(exc.code) from exc
        raise PipelineRunnerError("invalid_identity") from exc

    mode = payload.get("mode", "ordinary")
    if mode not in _MODES:
        raise PipelineRunnerError("invalid_execution_mode")
    lease = _mapping(payload.get("lease"), "lease")
    lease_owner = lease.get("owner")
    if not isinstance(lease_owner, str) or not _OWNER_RE.fullmatch(lease_owner):
        raise PipelineRunnerError("invalid_lease_owner")
    callback_owner = lease.get("callbackOwner", lease_owner)
    if callback_owner != lease_owner:
        raise PipelineRunnerError("late_lease_owner")
    if lease.get("status") != "ACTIVE":
        raise PipelineRunnerError("lease_not_active")
    expires_at = _timestamp(lease.get("expiresAt"), "lease_expires_at")
    if expires_at <= now:
        raise PipelineRunnerError("lease_expired")

    authorization = _mapping(payload.get("authorization"), "authorization")
    if authorization.get("verified") is not True:
        raise PipelineRunnerError("authorization_not_verified")
    if any(str(key).lower() in {"token", "secret", "password", "authorization"} for key in authorization):
        raise PipelineRunnerError("inline_credential_forbidden")
    authorization_kind = authorization.get("kind")
    if authorization_kind not in _AUTH_KINDS:
        raise PipelineRunnerError("invalid_authorization_kind")

    recovery_id: str | None = None
    recovery_plan_sha256: str | None = None
    recovery = payload.get("recovery")
    if mode == "recovery":
        recovery = _mapping(recovery, "recovery")
        recovery_id = recovery.get("recoveryId")
        if not isinstance(recovery_id, str) or not _OWNER_RE.fullmatch(recovery_id):
            raise PipelineRunnerError("invalid_recovery_id")
        recovery_plan_sha256 = _sha(recovery.get("planSha256"), "recovery_plan_sha256")
        if authorization_kind != "approved-recovery":
            raise PipelineRunnerError("recovery_authorization_required")
        if authorization.get("planSha256") != recovery_plan_sha256:
            raise PipelineRunnerError("recovery_plan_sha_mismatch")
    elif recovery is not None:
        raise PipelineRunnerError("ordinary_run_cannot_carry_recovery")
    elif authorization_kind != "existing-admin":
        raise PipelineRunnerError("ordinary_authorization_required")

    declared_lock = payload.get("dependencyLockSha256")
    if declared_lock != dependency_lock_sha256:
        raise PipelineRunnerError("dependency_lock_sha_mismatch")
    return PipelineExecutionContext(
        identity=identity,
        mode=mode,
        lease_owner=lease_owner,
        lease_expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        authorization_kind=authorization_kind,
        recovery_id=recovery_id,
        recovery_plan_sha256=recovery_plan_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
    )


def _stage_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("stages")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
        raise PipelineRunnerError("pipeline_stages_required")
    if len(set(raw)) != len(raw) or any(item not in APPLICATION_STAGES for item in raw):
        raise PipelineRunnerError("invalid_pipeline_stage")
    return tuple(raw)


def _stage_inputs(payload: Mapping[str, Any], stages: Sequence[str]) -> dict[str, str]:
    raw = _mapping(payload.get("stageInputs"), "stage_inputs")
    result: dict[str, str] = {}
    for stage in stages:
        result[stage] = _sha(raw.get(stage), f"{stage}_input_sha256")
    return result


def _load_checkpoints(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineRunnerError("checkpoint_state_invalid") from exc
    if isinstance(value, Mapping):
        value = value.get("checkpoints")
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise PipelineRunnerError("checkpoint_state_invalid")
    return list(value)


def _checkpoint_from_fixture(
    *,
    context: PipelineExecutionContext,
    stage: str,
    input_sha256: str,
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    if value.get("activeReleaseChanged") is True:
        raise PipelineRunnerError("candidate_cannot_change_active_release")
    evidence = value.get("evidence") if isinstance(value.get("evidence"), Mapping) else {}
    status = value.get("status")
    if status == "COMPLETED" and evidence.get("r2Status") == "SUCCEEDED" and evidence.get("d1Status") == "FAILED":
        raise PipelineRunnerError("partial_remote_side_effect_cannot_complete")
    try:
        checkpoint = build_checkpoint(
            identity=context.identity,
            stage=stage,
            status=status,
            input_sha256=input_sha256,
            output_sha256=value.get("outputSha256"),
            allowed_side_effects=value.get("allowedSideEffects", ()),
            evidence=_safe_json(evidence),
        )
    except (ApplicationContractError, TypeError) as exc:
        if isinstance(exc, ApplicationContractError):
            raise PipelineRunnerError(exc.code) from exc
        raise PipelineRunnerError("invalid_stage_checkpoint") from exc
    return checkpoint


def run_pipeline_request(
    payload: Mapping[str, Any],
    *,
    state_dir: Path,
    output_path: Path,
    producer_build_sha: str,
    dependency_lock_paths: Sequence[str | Path],
    project_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a candidate request through the same executor used by local/CI."""

    if not isinstance(payload, Mapping):
        raise PipelineRunnerError("pipeline_request_object_required")
    state_dir = state_dir.resolve()
    output_path = output_path.resolve()
    _assert_safe_output(state_dir / "checkpoints.json")
    _assert_safe_output(output_path)
    root = (project_root or Path.cwd()).resolve()
    lock_evidence = dependency_lock_digest(dependency_lock_paths, root=root)
    context = build_execution_context(
        payload,
        producer_build_sha=producer_build_sha,
        dependency_lock_sha256=lock_evidence["sha256"],
        now=now,
    )
    stages = _stage_names(payload)
    stage_inputs = _stage_inputs(payload, stages)
    stage_results = _mapping(payload.get("stageResults"), "stage_results")
    checkpoints_path = state_dir / "checkpoints.json"
    existing = _load_checkpoints(checkpoints_path)

    def stage_runner(stage: str, input_sha256: str) -> Mapping[str, Any]:
        value = stage_results.get(stage)
        if not isinstance(value, Mapping):
            raise PipelineRunnerError("stage_result_missing", stage)
        return _checkpoint_from_fixture(
            context=context,
            stage=stage,
            input_sha256=input_sha256,
            value=value,
        )

    def persist_progress(checkpoints: Sequence[Mapping[str, Any]]) -> None:
        _write_json_atomic(
            checkpoints_path,
            {
                "schemaVersion": EXECUTOR_SCHEMA_VERSION,
                "executorContractVersion": EXECUTOR_CONTRACT_VERSION,
                "identity": context.identity.as_dict(),
                "checkpoints": list(checkpoints),
            },
        )

    result = execute_application_plan(
        identity=context.identity,
        stages=stages,
        stage_inputs=stage_inputs,
        stage_runner=stage_runner,
        existing_checkpoints=existing,
        checkpoint_progress=persist_progress,
    )
    latest_data_date = payload.get("latestDataDate")
    if latest_data_date is not None:
        latest_data_date = _date(latest_data_date, "latest_data_date")
    report = {
        "schemaVersion": EXECUTOR_SCHEMA_VERSION,
        "executorContractVersion": EXECUTOR_CONTRACT_VERSION,
        "status": result["status"],
        "context": context.as_dict(),
        "dependencyLock": lock_evidence,
        "plan": result,
        "latestDataDate": latest_data_date,
        "historyPolicy": "historical_target_does_not_rewrite_latest",
        "sideEffects": {
            "remoteWrites": False,
            "workflowDispatch": False,
            "activeReleaseChanged": False,
            "productionEntryEnabled": False,
        },
    }
    _write_json_atomic(output_path, report)
    return report


__all__ = [
    "EXECUTOR_CONTRACT_VERSION",
    "EXECUTOR_SCHEMA_VERSION",
    "PipelineExecutionContext",
    "PipelineRunnerError",
    "build_execution_context",
    "dependency_lock_digest",
    "run_pipeline_request",
]
