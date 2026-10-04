"""Candidate cross-environment entry for the shared pipeline executor.

The CLI and manual GitHub workflow remain fixture-only.  An in-process caller
may explicitly inject side-effect-classified stage adapters, but only after an
independent control-plane verifier accepts the execution identity.  The
current core-collection adapter is bounded and local-output-only; no current
caller wires it to production scheduling or publishing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
from .pipeline_checkpoint_bundle import build_stage_bundle, restore_stage_bundle, verify_stage_bundle


EXECUTOR_SCHEMA_VERSION = 1
EXECUTOR_CONTRACT_VERSION = "pipeline-executor-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MODES = {"ordinary", "recovery"}
_AUTH_KINDS = {"existing-admin", "approved-recovery"}
_PROTECTED_PATHS = {"memory"}
_ALLOWED_ADAPTER_EFFECTS = {
    "official_source_requests",
    "filesystem:isolated_stage_workspace",
    "candidate_publication_requests",
}


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
    parts = path.resolve(strict=False).parts
    if any(part in _PROTECTED_PATHS for part in parts):
        raise PipelineRunnerError("protected_output_path")
    for index, part in enumerate(parts[:-2]):
        if (
            part == "reports"
            and parts[index + 1] == "phase0"
            and parts[index + 2].startswith("latest-")
        ):
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
    lease_attempt_count: int | None = None
    lease_check: Callable[[], None] | None = field(default=None, repr=False, compare=False)

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
        if self.lease_attempt_count is not None:
            result["lease"]["attemptCount"] = self.lease_attempt_count
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
    attempt = lease.get("attemptCount")
    if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1):
        raise PipelineRunnerError("invalid_lease_attempt_count")
    return PipelineExecutionContext(
        identity=identity,
        mode=mode,
        lease_owner=lease_owner,
        lease_expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        authorization_kind=authorization_kind,
        recovery_id=recovery_id,
        recovery_plan_sha256=recovery_plan_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
        lease_attempt_count=attempt,
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


def _checkpoint_from_stage_result(
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
    stage_adapters: Mapping[str, Any] | None = None,
    authorization_verifier: Callable[[PipelineExecutionContext], bool] | None = None,
    lease_guard: Any | None = None,
    resume_bundles: Mapping[str, tuple[bytes, str]] | None = None,
    export_bundles: bool = False,
    checkpoint_transport: Any | None = None,
    resume_references: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run fixture stages or explicitly injected, verified stage adapters.

    The CLI/workflow use fixture mode only.  An in-process adapter caller must
    provide both a complete adapter map and an independent control-plane
    authorization verifier before any stage can perform side effects.
    """

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
    adapter_mode = stage_adapters is not None
    if (resume_bundles is not None or export_bundles or checkpoint_transport is not None or resume_references is not None) and (not adapter_mode or lease_guard is None):
        raise PipelineRunnerError("bundle_operations_require_adapter_and_lease_guard")
    if resume_references is not None and (checkpoint_transport is None or resume_bundles is not None
        or not isinstance(resume_references, Mapping) or set(resume_references) - set(stages)):
        raise PipelineRunnerError("invalid_durable_resume_references")
    if checkpoint_transport is not None and not (export_bundles or resume_references is not None):
        raise PipelineRunnerError("checkpoint_transport_operation_required")
    if resume_bundles is not None and (not isinstance(resume_bundles, Mapping) or set(resume_bundles) - set(stages)):
        raise PipelineRunnerError("invalid_resume_bundle_stage_set")
    stage_results: Mapping[str, Any] = {}
    stage_configs: Mapping[str, Any] = {}
    adapter_effects: set[str] = set()
    source_request_count = 0
    if adapter_mode:
        if lease_guard is not None and authorization_verifier is not None:
            raise PipelineRunnerError("ambiguous_live_authorization_verifier")
        if authorization_verifier is None and lease_guard is None:
            raise PipelineRunnerError("live_authorization_verifier_required")
        try:
            authorization_verified = lease_guard.verify(context) if lease_guard is not None else authorization_verifier(context)
        except PipelineRunnerError:
            raise
        except Exception as exc:
            raise PipelineRunnerError("live_authorization_verification_failed") from exc
        if authorization_verified is not True:
            raise PipelineRunnerError("live_authorization_verification_failed")
        if lease_guard is not None:
            context = replace(context, lease_check=lease_guard.check)
        if "stageResults" in payload:
            raise PipelineRunnerError("fixture_results_forbidden_with_stage_adapters")
        stage_configs = _mapping(payload.get("stageConfigs"), "stage_configs")
        if set(stage_configs) != set(stages):
            raise PipelineRunnerError("stage_config_set_mismatch")
        if set(stage_adapters or {}) != set(stages):
            raise PipelineRunnerError("stage_adapter_set_mismatch")
        for stage in stages:
            config = _mapping(stage_configs[stage], f"{stage}_config")
            if canonical_sha256(config) != stage_inputs[stage]:
                raise PipelineRunnerError("stage_config_input_digest_mismatch", stage)
            adapter = (stage_adapters or {}).get(stage)
            if not callable(getattr(adapter, "run", None)):
                raise PipelineRunnerError("stage_adapter_run_required", stage)
            if not callable(getattr(adapter, "verify_checkpoint", None)):
                raise PipelineRunnerError("stage_adapter_checkpoint_verifier_required", stage)
            effects = getattr(adapter, "allowed_side_effects", None)
            if (
                not isinstance(effects, (tuple, list, set, frozenset))
                or any(not isinstance(effect, str) for effect in effects)
            ):
                raise PipelineRunnerError("stage_adapter_effect_contract_required", stage)
            effect_set = set(effects)
            if "candidate_publication_requests" in effect_set and (stage != "publish" or context.mode != "ordinary"):
                raise PipelineRunnerError("candidate_publication_effect_scope_invalid", stage)
            if not effect_set or not effect_set.issubset(_ALLOWED_ADAPTER_EFFECTS):
                raise PipelineRunnerError("unsupported_stage_adapter_effect", stage)
            adapter_effects.update(effect_set)
    else:
        if authorization_verifier is not None or lease_guard is not None or "stageConfigs" in payload:
            raise PipelineRunnerError("stage_adapter_configuration_without_adapters")
        stage_results = _mapping(payload.get("stageResults"), "stage_results")
    checkpoints_path = state_dir / "checkpoints.json"
    existing = _load_checkpoints(checkpoints_path)
    bundle_descriptors: dict[str, Any] = {}
    if resume_references:
        if existing:
            raise PipelineRunnerError("resume_bundle_local_state_conflict")
        resume_bundles = {}
        for stage, digest in resume_references.items():
            _sha(digest, "durable_bundle_sha256")
            data = checkpoint_transport.download(context=context, stage=stage,
                input_sha256=stage_inputs[stage], sha256=digest)
            resume_bundles[stage] = (data, digest)
    if resume_bundles:
        if existing:
            raise PipelineRunnerError("resume_bundle_local_state_conflict")
        for stage in stages:
            if stage not in resume_bundles:
                continue
            reference = resume_bundles[stage]
            if not isinstance(reference, tuple) or len(reference) != 2:
                raise PipelineRunnerError("invalid_resume_bundle_reference")
            adapter = (stage_adapters or {})[stage]
            try:
                prior, _ = verify_stage_bundle(reference[0], sha256=reference[1], identity=context.identity,
                                               stage=stage, input_sha256=stage_inputs[stage])
                binding = prior.get("evidence", {}).get("runnerBinding", {})
                expected_binding = {"dependencyLockSha256": context.dependency_lock_sha256,
                                    "mode": context.mode, "recoveryId": context.recovery_id,
                                    "recoveryPlanSha256": context.recovery_plan_sha256}
                if any(binding.get(key) != value for key, value in expected_binding.items()):
                    raise PipelineRunnerError("resume_bundle_execution_binding_mismatch")
                checkpoint = restore_stage_bundle(
                    reference[0], sha256=reference[1], identity=context.identity,
                    stage=stage, input_sha256=stage_inputs[stage],
                    workspace=state_dir / "stages" / stage / stage_inputs[stage],
                    confirm_lease=lease_guard.confirm, check_lease=lease_guard.check,
                    verify_output=adapter.verify_checkpoint,
                )
            except ValueError as exc:
                raise PipelineRunnerError("resume_bundle_verification_failed") from exc
            existing.append(checkpoint)
    if adapter_mode:
        for checkpoint in existing:
            stage = checkpoint.get("stage")
            if stage not in stages or checkpoint.get("status") != "COMPLETED":
                continue
            adapter = (stage_adapters or {}).get(stage)
            verifier = getattr(adapter, "verify_checkpoint")
            input_sha256 = stage_inputs[stage]
            workspace = (state_dir / "stages" / stage / input_sha256).resolve(strict=False)
            try:
                workspace.relative_to(state_dir)
            except ValueError as exc:
                raise PipelineRunnerError("stage_workspace_path_escape", stage) from exc
            try:
                verified = verifier(
                    checkpoint=checkpoint,
                    identity=context.identity,
                    input_sha256=input_sha256,
                    workspace=workspace,
                )
            except Exception as exc:
                raise PipelineRunnerError("stage_checkpoint_evidence_verification_failed", stage) from exc
            if verified is not True:
                raise PipelineRunnerError("stage_checkpoint_evidence_mismatch", stage)

    def stage_runner(stage: str, input_sha256: str) -> Mapping[str, Any]:
        nonlocal source_request_count
        if adapter_mode:
            if context.lease_check is not None:
                context.lease_check()
            adapter = (stage_adapters or {}).get(stage)
            run = getattr(adapter, "run", None)
            config = stage_configs.get(stage)
            if not isinstance(config, Mapping):
                raise PipelineRunnerError("stage_config_missing", stage)
            workspace = state_dir / "stages" / stage / input_sha256
            try:
                workspace = workspace.resolve(strict=False)
                workspace.relative_to(state_dir)
            except ValueError as exc:
                raise PipelineRunnerError("stage_workspace_path_escape", stage) from exc
            _assert_safe_output(workspace / "adapter-receipt.json")
            try:
                value = run(
                    context=context,
                    input_sha256=input_sha256,
                    config=config,
                    workspace=workspace,
                )
            except PipelineRunnerError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", code):
                    raise PipelineRunnerError(code) from exc
                raise PipelineRunnerError("stage_adapter_failed", type(exc).__name__) from exc
            if context.lease_check is not None:
                context.lease_check()
            if isinstance(value, Mapping):
                value = dict(value)
                value["evidence"] = dict(value.get("evidence") or {})
                value["evidence"]["runnerBinding"] = {
                    "dependencyLockSha256": context.dependency_lock_sha256,
                    "mode": context.mode, "recoveryId": context.recovery_id,
                    "recoveryPlanSha256": context.recovery_plan_sha256,
                    "leaseOwner": context.lease_owner, "attemptCount": context.lease_attempt_count,
                }
            if "official_source_requests" in getattr(adapter, "allowed_side_effects", ()):
                evidence = value.get("evidence") if isinstance(value, Mapping) else None
                observed = evidence.get("sourceRequestCount") if isinstance(evidence, Mapping) else None
                budget = evidence.get("sourceRequestBudget") if isinstance(evidence, Mapping) else None
                if (
                    isinstance(observed, bool)
                    or not isinstance(observed, int)
                    or not 0 <= observed <= 100
                    or isinstance(budget, bool)
                    or not isinstance(budget, int)
                    or not 1 <= budget <= 100
                    or observed > budget
                ):
                    raise PipelineRunnerError("stage_source_request_evidence_invalid", stage)
                source_request_count += observed
        else:
            value = stage_results.get(stage)
            if not isinstance(value, Mapping):
                raise PipelineRunnerError("stage_result_missing", stage)
        return _checkpoint_from_stage_result(
            context=context,
            stage=stage,
            input_sha256=input_sha256,
            value=value,
        )

    def persist_progress(checkpoints: Sequence[Mapping[str, Any]]) -> None:
        if lease_guard is not None:
            lease_guard.confirm()
        if export_bundles:
            for checkpoint in checkpoints:
                stage = checkpoint["stage"]
                if checkpoint.get("status") != "COMPLETED" or stage in bundle_descriptors:
                    continue
                binding = checkpoint.get("evidence", {}).get("runnerBinding", {})
                expected_binding = {"dependencyLockSha256": context.dependency_lock_sha256,
                                    "mode": context.mode, "recoveryId": context.recovery_id,
                                    "recoveryPlanSha256": context.recovery_plan_sha256}
                if any(binding.get(key) != value for key, value in expected_binding.items()):
                    raise PipelineRunnerError("bundle_export_execution_binding_missing")
                workspace = state_dir / "stages" / stage / stage_inputs[stage]
                adapter = (stage_adapters or {})[stage]
                if adapter.verify_checkpoint(checkpoint=checkpoint, identity=context.identity,
                                             input_sha256=stage_inputs[stage], workspace=workspace) is not True:
                    raise PipelineRunnerError("bundle_export_adapter_evidence_mismatch")
                data, digest = build_stage_bundle(workspace=workspace, checkpoint=checkpoint,
                                                 identity=context.identity, stage=stage,
                                                 input_sha256=stage_inputs[stage])
                lease_guard.confirm()
                bundle_dir = state_dir / "bundles"
                _assert_safe_output(bundle_dir / f"{digest}.json")
                if any(path.is_symlink() for path in (bundle_dir, *bundle_dir.parents)):
                    raise PipelineRunnerError("bundle_export_symlink_forbidden")
                bundle_dir.mkdir(parents=True, exist_ok=True)
                bundle_path = bundle_dir / f"{digest}.json"
                if bundle_path.exists():
                    if bundle_path.is_symlink() or bundle_path.stat().st_size != len(data) or bundle_path.read_bytes() != data:
                        raise PipelineRunnerError("bundle_export_collision")
                else:
                    with bundle_path.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                bundle_descriptors[stage] = {"path": str(bundle_path), "sha256": digest, "bytes": len(data)}
                if checkpoint_transport is not None:
                    descriptor = checkpoint_transport.upload(context=context, stage=stage,
                        input_sha256=stage_inputs[stage], data=data, sha256=digest)
                    bundle_descriptors[stage]["remote"] = descriptor
            lease_guard.confirm()
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
    if lease_guard is not None:
        lease_guard.confirm()
    if latest_data_date is not None:
        latest_data_date = _date(latest_data_date, "latest_data_date")
    report = {
        "schemaVersion": EXECUTOR_SCHEMA_VERSION,
        "executorContractVersion": EXECUTOR_CONTRACT_VERSION,
        "executionMode": "stage_adapter" if adapter_mode else "fixture",
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
            "officialSourceRequests": source_request_count > 0,
            "officialSourceRequestCount": source_request_count,
            "officialSourceRequestsAllowed": "official_source_requests" in adapter_effects,
            "isolatedFilesystemWrites": True,
        },
    }
    if lease_guard is not None:
        report["leaseEvidence"] = lease_guard.evidence()
        report["sideEffects"]["remoteWrites"] = lease_guard.renewal_count > 0
    if resume_bundles is not None or export_bundles or checkpoint_transport is not None:
        report["checkpointBundles"] = {"exports": bundle_descriptors, "restoredStages": sorted(resume_bundles or {}),
                                       "durableTransport": "in_process_candidate" if checkpoint_transport is not None else "not_connected"}
    if checkpoint_transport is not None:
        report["checkpointTransportEvidence"] = checkpoint_transport.evidence()
        report["sideEffects"]["remoteWrites"] |= checkpoint_transport.upload_attempts > 0
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
