"""Offline checkpoint sizing and budget diagnostics, not execution authority.

Reuses the immutable bundle and resource contracts. No credentials, filesystem
writes, provider calls, reservations, or lifecycle mutations are performed here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .pipeline_application import ApplicationIdentity, build_application_plan
from .pipeline_checkpoint_bundle import verify_stage_bundle
from .pipeline_checkpoint_transport import MAX_OBJECT_BYTES
from .pipeline_checkpoint_lifecycle import checkpoint_lifecycle_preflight
from .resource_budget import BudgetVector, ResourceLimits, evaluate_budget


def recovery_control_preflight(*, checkpoint_requests: int | None,
    ledger_operations: int | None, runner_confirmations: int | None,
    renewal_upper_bound: int | None, lease_request_limit: int = 32,
    approved_request_limit: int = 32, server_fenced_checks: bool = False) -> dict[str, Any]:
    """Pure request envelope for existing recovery guard/ledger/checkpoints.

    Counts include bounded retries/readbacks. This checks a declared envelope,
    not that a caller supplied all execution branches. Provider row counts and
    account capacity cannot be inferred from HTTP request counts.
    """
    components = dict(checkpointRequests=checkpoint_requests,
        ledgerOperations=ledger_operations, runnerConfirmations=runner_confirmations,
        renewalUpperBound=renewal_upper_bound)
    if type(server_fenced_checks) is not bool:
        raise ValueError("recovery_check_policy_invalid")
    for count in components.values():
        if count is not None and (type(count) is not int or not 0 <= count <= 10_000):
            raise ValueError("recovery_request_count_invalid")
    if (type(lease_request_limit) is not int or not 1 <= lease_request_limit <= 32
            or type(approved_request_limit) is not int or not 1 <= approved_request_limit <= 10_000):
        raise ValueError("recovery_request_limit_invalid")
    limit = min(lease_request_limit, approved_request_limit)
    unknown = sorted(name for name, count in components.items() if count is None)
    lease = worker = None
    blockers = []
    if unknown:
        blockers.append("recovery_control_counts_unknown")
    else:
        # Legacy ledger GET + POST each confirm before/after (4 confirms).
        # Server-fenced mode uses local checks instead; due renewals are counted
        # separately. Transport before/after remains fresh in both modes.
        # Recovery confirm fetches approval + current row (2 HTTP requests).
        # Standalone ledger transitions use the same two-request CAS protocol.
        # Each possible renewal adds at most 3 requests conservatively, rather
        # than subtracting a replaced confirmation from the estimate.
        lease = (2 + (4 if server_fenced_checks else 12) * checkpoint_requests
                 + (0 if server_fenced_checks else 8) * ledger_operations
                 + 2 * runner_confirmations + 3 * renewal_upper_bound)
        worker = lease + 3 * checkpoint_requests + 2 * ledger_operations
        if lease > limit:
            blockers.append("recovery_lease_request_envelope_exceeded")
    return {"schemaVersion": 1, "kind": "offline_recovery_control_preflight",
        "status": "BLOCKED" if blockers else "DECLARED_ENVELOPE_CHECKED",
        "components": components, "unknownComponents": unknown,
        "leaseCheckPolicy": "server_fenced" if server_fenced_checks else "fresh_confirm",
        "leaseRequestsUpperBound": lease, "workerRequestsUpperBound": worker,
        "effectiveLeaseRequestLimit": limit, "blockers": blockers,
        "basis": "estimated_declared_envelope", "liveExecutionAllowed": False,
        "providerRows": "UNKNOWN", "accountRemaining": "UNKNOWN"}


def pipeline_task_preflight(
    *, identity: ApplicationIdentity, stages: list[str], stage_inputs: dict[str, str],
    bundles: dict[str, tuple[bytes, str]], stage_costs: dict[str, BudgetVector],
    overhead_costs: dict[str, BudgetVector], limits: ResourceLimits,
    measured: BudgetVector, reserved: BudgetVector, now: datetime,
    measurement_ttl_seconds: int, lifecycle_evidence: dict[str, Any] | None = None,
    recovery_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate one declared plan, never reserve resources or authorize a run.

    Stage estimates must include their bounded retries. Explicit overhead covers
    lease/control I/O, checkpoint transport and platform runtime. Known numbers
    are caller evidence, not measured provider usage or completeness proof.
    Missing components stay UNKNOWN; no implicit zero-cost stage is invented.
    Exact bundles may be prospective offline outputs or archived checkpoints.
    """
    plan = build_application_plan(identity, stages, stage_inputs=stage_inputs)
    if set(stage_inputs) != set(stages):
        raise ValueError("task_stage_input_set_mismatch")
    overhead_names = {"lease_control", "checkpoint_io", "platform_runtime"}
    if set(bundles) - set(stages) or set(stage_costs) - set(stages) or set(overhead_costs) - overhead_names:
        raise ValueError("task_component_set_mismatch")
    if isinstance(measurement_ttl_seconds, bool) or not isinstance(measurement_ttl_seconds, int) or measurement_ttl_seconds <= 0:
        raise ValueError("positive_measurement_ttl_required")
    costs = {f"stage.{stage}": stage_costs.get(stage, BudgetVector.unknown(reason="stage_cost_missing"))
             for stage in stages}
    costs.update({f"overhead.{name}": overhead_costs.get(name, BudgetVector.unknown(reason="overhead_cost_missing"))
                  for name in sorted(overhead_names)})
    if any(not isinstance(cost, BudgetVector) for cost in costs.values()):
        raise ValueError("task_cost_vector_required")
    iterator = iter(costs.values())
    total_cost = next(iterator)
    for cost in iterator:
        total_cost = total_cost.add(cost, scope="pipeline_task")
    budget = evaluate_budget(limits=limits, measured=measured, reserved=reserved,
        task_cost=total_cost, now=now, measurement_ttl_seconds=measurement_ttl_seconds)
    missing_bundles = [stage for stage in stages if stage not in bundles]
    missing_costs = [stage for stage in stages if stage not in stage_costs]
    missing_overhead = sorted(overhead_names - set(overhead_costs))
    blockers = []
    if missing_bundles:
        blockers.append("task_checkpoint_evidence_incomplete")
    if missing_costs or missing_overhead:
        blockers.append("task_cost_components_incomplete")
    if budget["decision"] not in {"allow", "warn"}:
        blockers.append("resource_" + budget["decision"])
    artifacts = {}
    floors = {"r2Bytes": 0, "r2ClassA": 0, "r2ClassB": 0, "workerRequests": 0}
    # Maximum six declared application stages; existing per-bundle limits apply.
    for stage in stages:
        if stage not in bundles:
            continue
        bundle = bundles[stage]
        if not isinstance(bundle, tuple) or len(bundle) != 2:
            raise ValueError("task_checkpoint_tuple_required")
        data, digest = bundle
        report = checkpoint_preflight(data=data, sha256=digest, identity=identity,
            stage=stage, input_sha256=stage_inputs[stage], limits=limits,
            measured=measured, reserved=reserved, task_cost=total_cost, now=now,
            measurement_ttl_seconds=measurement_ttl_seconds, lifecycle_evidence=lifecycle_evidence)
        artifacts[stage] = {"bundleSha256": digest, **report["artifact"]}
        if not report["artifact"]["fitsTransport"]:
            blockers.append("transport_object_size_exceeded:" + stage)
        for dimension, floor in report["transportCostFloors"].items():
            floors[dimension] += floor
    lifecycle = checkpoint_lifecycle_preflight(lifecycle_evidence, now=now)
    if lifecycle["status"] == "BLOCKED":
        blockers.append("checkpoint_lifecycle_inputs_incomplete")
    checkpoint_cost = overhead_costs.get("checkpoint_io", BudgetVector.unknown())
    understated = sorted(name for name, floor in floors.items()
        if checkpoint_cost.metrics[name].value is not None and checkpoint_cost.metrics[name].value < floor)
    if understated:
        blockers.append("checkpoint_cost_below_aggregate_transport_floor")
    control = None
    if recovery_control is not None:
        control = recovery_control_preflight(**recovery_control)
        blockers.extend(control["blockers"])
        worker_floor = control["workerRequestsUpperBound"]
        control_cost = overhead_costs.get("lease_control", BudgetVector.unknown()).add(
            checkpoint_cost, scope="recovery_control")
        worker_cost = control_cost.metrics["workerRequests"].value
        if worker_floor is not None and worker_cost is not None and worker_cost < worker_floor:
            blockers.append("recovery_control_worker_cost_understated")
    return {"schemaVersion": 1, "kind": "offline_pipeline_task_preflight",
        "status": "BLOCKED" if blockers else "LOCAL_INPUTS_CHECKED",
        "plan": plan, "artifacts": artifacts,
        "totals": {"encodedBytes": sum(value["encodedBytes"] for value in artifacts.values()),
                   "decodedBytes": sum(value["decodedBytes"] for value in artifacts.values()),
                   "fileCount": sum(value["fileCount"] for value in artifacts.values())},
        "costComponents": {name: cost.as_dict() for name, cost in costs.items()},
        "missingBundles": missing_bundles, "missingStageCosts": missing_costs,
        "missingOverheadCosts": missing_overhead, "budget": budget,
        "transportCostFloors": floors, "understatedDimensions": understated,
        "lifecycle": lifecycle, "blockers": sorted(set(blockers)),
        "recoveryControl": control,
        "liveExecutionAllowed": False, "resourceReservationPerformed": False,
        "sideEffects": {"remoteReads": False, "remoteWrites": False, "filesystemWrites": False},
        "remainingRequirements": ["complete_bounded_retry_and_lease_cost_proof",
            "authenticated_provider_usage", "atomic_shared_resource_reservation",
            "verified_checkpoint_lifecycle", "runtime_authorization"]}


def checkpoint_preflight(
    *, data: bytes, sha256: str, identity: ApplicationIdentity, stage: str,
    input_sha256: str, limits: ResourceLimits, measured: BudgetVector,
    reserved: BudgetVector, task_cost: BudgetVector, now: datetime,
    measurement_ttl_seconds: int,
    lifecycle_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect exact bytes and complete caller-supplied task upper bounds.

    Task cost must cover the entire task (including lease checks/renewals), not
    just PUT. Missing evidence remains unknown. The operation floors below are
    necessary but not sufficient: they cannot prove a task estimate complete.
    """
    if isinstance(measurement_ttl_seconds, bool) or not isinstance(measurement_ttl_seconds, int) or measurement_ttl_seconds <= 0:
        raise ValueError("positive_measurement_ttl_required")
    _, files = verify_stage_bundle(data, sha256=sha256, identity=identity,
                                  stage=stage, input_sha256=input_sha256)
    budget = evaluate_budget(limits=limits, measured=measured, reserved=reserved,
                             task_cost=task_cost, now=now,
                             measurement_ttl_seconds=measurement_ttl_seconds)
    # One immutable POST, one server readback, plus one possible ambiguity GET.
    # GET itself reads R2 too. These are floors, never a full-task estimate.
    floors = {"r2Bytes": len(data), "r2ClassA": 1, "r2ClassB": 2, "workerRequests": 2}
    understated = sorted(name for name, floor in floors.items()
                         if task_cost.metrics[name].value is not None
                         and task_cost.metrics[name].value < floor)
    blockers = []
    lifecycle = checkpoint_lifecycle_preflight(lifecycle_evidence, now=now)
    if lifecycle["status"] == "BLOCKED":
        blockers.append("checkpoint_lifecycle_inputs_incomplete")
    if len(data) > MAX_OBJECT_BYTES:
        blockers.append("transport_object_size_exceeded")
    if budget["decision"] not in {"allow", "warn"}:
        blockers.append("resource_" + budget["decision"])
    if understated:
        blockers.append("task_cost_below_transport_floor")
    return {
        "schemaVersion": 1, "kind": "offline_checkpoint_preflight",
        "status": "BLOCKED" if blockers else "LOCAL_INPUTS_CHECKED",
        "runKey": identity.run_key, "targetDate": identity.target_date,
        "stage": stage, "bundleSha256": sha256,
        "artifact": {"encodedBytes": len(data), "decodedBytes": sum(map(len, files.values())),
                     "fileCount": len(files), "transportMaxBytes": MAX_OBJECT_BYTES,
                     "fitsTransport": len(data) <= MAX_OBJECT_BYTES},
        "budget": budget, "transportCostFloors": floors,
        "lifecycle": lifecycle,
        "understatedDimensions": understated, "blockers": blockers,
        "liveExecutionAllowed": False,
        "remainingRequirements": ["verified_checkpoint_lifecycle", "atomic_shared_resource_reservation",
                                  "complete_task_cost_proof", "runtime_authorization"],
        "sideEffects": {"remoteReads": False, "remoteWrites": False, "filesystemWrites": False},
    }
