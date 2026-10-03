"""Offline checkpoint sizing and budget diagnostics, not execution authority.

Reuses the immutable bundle and resource contracts. No credentials, filesystem
writes, provider calls, reservations, or lifecycle mutations are performed here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .pipeline_application import ApplicationIdentity
from .pipeline_checkpoint_bundle import verify_stage_bundle
from .pipeline_checkpoint_transport import MAX_OBJECT_BYTES
from .pipeline_checkpoint_lifecycle import checkpoint_lifecycle_preflight
from .resource_budget import BudgetVector, ResourceLimits, evaluate_budget


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
