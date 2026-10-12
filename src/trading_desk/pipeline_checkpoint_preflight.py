"""Offline checkpoint sizing and budget diagnostics, not execution authority.

Reuses the immutable bundle and resource contracts. No credentials, filesystem
writes, provider calls, reservations, or lifecycle mutations are performed here.
"""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from math import ceil
import re
from typing import Any

from .pipeline_application import ApplicationIdentity, build_application_plan
from .pipeline_checkpoint_bundle import verify_stage_bundle
from .pipeline_checkpoint_transport import MAX_OBJECT_BYTES
from .pipeline_checkpoint_lifecycle import checkpoint_lifecycle_preflight
from .resource_budget import BudgetVector, ResourceLimits, evaluate_budget


@dataclass(frozen=True)
class FreshCandidateRequestProfile:
    """Opt-in local ceilings, not quota, approval, lease or effect authority.

    Narrow scope: fresh ordinary build/verify/publish, both exports, no resume.
    Legacy clients stay at 32. A future entry still needs account admission and
    measured platform cost; constructing this value cannot authorize a run.
    """
    input_sha256: str
    artifact_count: int
    maximum_seconds: int = 300

    def __post_init__(self):
        if not isinstance(self.input_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.input_sha256):
            raise ValueError("candidate_profile_input_invalid")
        # Same 20-artifact schema ceiling enforced by the candidate endpoint;
        # client ceilings below are derived, not the legacy arbitrary 40 calls.
        if type(self.artifact_count) is not int or not 1 <= self.artifact_count <= 20:
            raise ValueError("candidate_profile_artifact_limit")
        if type(self.maximum_seconds) is not int or not 1 <= self.maximum_seconds <= 300:
            raise ValueError("candidate_profile_deadline_invalid")

    def envelope(self):
        normal_budget = 4 + 2 * 3 + 3 * self.artifact_count + 2 * 3
        # At most one refresh+retry per mutation, including the two bounded
        # reconciliation admissions. First snapshot is read-only.
        conflicts = normal_budget - 1 + 2 + 1
        # The guard rejects <30s remaining; renewal is due no earlier than
        # half that minimum (15s). Count conservative replacement renewals
        # additively, even though confirm normally replaces one status GET.
        return fresh_candidate_request_envelope(stage_count=3, artifact_count=self.artifact_count,
            renewal_requests=ceil(self.maximum_seconds / 15), revision_conflicts=conflicts,
            checkpoint_readbacks=1, candidate_reconciliations=1, failure_settlements=1)


@dataclass(frozen=True)
class FreshRecoveryCandidateRequestProfile(FreshCandidateRequestProfile):
    """Same candidate graph, additionally fenced by exact existing approval.

    Approval and lease reads remain fresh. This only derives their extra
    requests; the approved plan must cover the entire envelope, not each
    individual client's limit in isolation.
    """
    recovery_id: str = ""
    plan_sha256: str = ""

    def __post_init__(self):
        super().__post_init__()
        if (not isinstance(self.recovery_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.recovery_id)
                or not isinstance(self.plan_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.plan_sha256)):
            raise ValueError("candidate_profile_recovery_identity_invalid")

    def envelope(self):
        report = super().envelope()
        # Every recovery confirmation is approval GET + execution-row GET;
        # renewal adds one POST and then both fresh GETs.
        report["normalRequests"]["lease"] *= 2
        renewals = report["faultRequestsUpper"]["renewalRequests"]
        report["requestsUpper"]["lease"] = 2 * (report["requestsUpper"]["lease"] - renewals) + 3 * renewals
        report["normalWorkerRequests"] = sum(report["normalRequests"].values())
        report["workerRequestsUpper"] = sum(report["requestsUpper"].values())
        report["kind"] = "fresh_recovery_v2_candidate_request_envelope"
        report["scope"] = "fresh_approved_dashboard_publish_with_both_exports_only"
        return report


def _resume_envelope(report, *, recovery):
    """One bounded second attempt, inheriting—not recreating—the first hold.

    At most one handoff and three bundle downloads, six restore confirmations,
    one prior-operation receipt lookup and one atomic adoption. Fresh execution
    remains a conservative envelope for the remaining stages. Unknown prior
    effect outcomes remain held; this does not grant their re-execution.
    """
    first_upper = report["workerRequestsUpper"]
    first_components = dict(report["requestsUpper"])
    extra_lease = 28 if recovery else 14
    report["normalRequests"]["lease"] += extra_lease
    report["normalRequests"]["budget"] += 6
    report["normalRequests"]["checkpoint"] += 4
    report["requestsUpper"]["lease"] += extra_lease
    # Six extra budget requests, each conservatively allowed one CAS refresh.
    report["requestsUpper"]["budget"] += 18
    report["requestsUpper"]["checkpoint"] += 4
    report["normalWorkerRequests"] = sum(report["normalRequests"].values())
    report["workerRequestsUpper"] = sum(report["requestsUpper"].values())
    report["taskWorkerRequestsUpper"] = first_upper + report["workerRequestsUpper"]
    report["taskRequestsUpper"] = {name: first_components[name] + count
        for name, count in report["requestsUpper"].items()}
    report["kind"] = "second_attempt_candidate_request_envelope"
    report["scope"] = "exact_remote_handoff_second_attempt_only"
    return report


@dataclass(frozen=True)
class ResumeCandidateRequestProfile(FreshCandidateRequestProfile):
    def envelope(self):
        return _resume_envelope(super().envelope(), recovery=False)


@dataclass(frozen=True)
class ResumeRecoveryCandidateRequestProfile(FreshRecoveryCandidateRequestProfile):
    def envelope(self):
        return _resume_envelope(super().envelope(), recovery=True)


def candidate_task_cost_upper(*, profile, stage_costs, request_costs, runtime_cost):
    """Aggregate declared seven-dimensional bounds, not provider usage.

    Each request estimate covers the most expensive supported operation for
    that client, including bounded fault paths. Resume includes both attempts;
    it must not create a new allowance. Missing evidence stays UNKNOWN. This
    pure calculation performs no admission or remote operation.
    """
    if type(profile) not in {FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile,
                            ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile}:
        raise ValueError("task_cost_profile_invalid")
    if set(stage_costs) != {"build", "verify", "publish"} or set(request_costs) != {
            "lease", "budget", "candidate", "checkpoint"}:
        raise ValueError("task_cost_component_set_invalid")
    vectors = [*stage_costs.values(), *request_costs.values(), runtime_cost]
    if any(not isinstance(vector, BudgetVector) for vector in vectors):
        raise ValueError("task_cost_vector_required")
    from .resource_budget import RESOURCE_DIMENSIONS
    report = profile.envelope()
    counts = report.get("taskRequestsUpper", report["requestsUpper"])
    stage_attempts = 2 if "taskRequestsUpper" in report else 1
    values = {}
    for dimension in RESOURCE_DIMENSIONS:
        components = [vector.metrics[dimension].value for vector in vectors]
        if any(value is None for value in components):
            values[dimension] = None
        else:
            values[dimension] = (stage_attempts * sum(vector.metrics[dimension].value for vector in stage_costs.values())
                + sum(request_costs[name].metrics[dimension].value * count for name, count in counts.items())
                + runtime_cost.metrics[dimension].value)
    return BudgetVector.estimated(values, scope="pipeline_task_declared_upper")


def fresh_candidate_request_envelope(*, stage_count: int, artifact_count: int,
    renewal_requests: int | None, revision_conflicts: int | None,
    checkpoint_readbacks: int | None, candidate_reconciliations: int | None,
    failure_settlements: int | None = 0) -> dict[str, Any]:
    """Count the current fresh ordinary v2 runner, with both exports enabled.

    Not valid for resume, recovery, legacy ledgers, or arbitrary adapters. This
    is a diagnostic, NOT a new permission or a replacement for account usage.
    A missing fault envelope stays unknown. Counts include the final UNKNOWN
    settlement; unsuccessful effects must not free their reservation.
    """
    if type(stage_count) is not int or not 1 <= stage_count <= 6:
        raise ValueError("candidate_envelope_stage_count_invalid")
    if type(artifact_count) is not int or not 1 <= artifact_count <= 20:
        raise ValueError("candidate_envelope_artifact_count_invalid")
    faults = dict(renewalRequests=renewal_requests, revisionConflicts=revision_conflicts,
        checkpointReadbacks=checkpoint_readbacks, candidateReconciliations=candidate_reconciliations,
        failureSettlements=failure_settlements)
    if failure_settlements is not None and (type(failure_settlements) is not int or not 0 <= failure_settlements <= 1):
        raise ValueError("candidate_envelope_failure_settlement_invalid")
    for value in faults.values():
        if value is not None and (type(value) is not int or not 0 <= value <= 256):
            raise ValueError("candidate_envelope_fault_count_invalid")
    # Candidate: upload, exact readback, and export-time checkpoint verification.
    candidate = 3 * artifact_count
    # One bundle and one cumulative handoff per completed stage.
    checkpoint = 2 * stage_count
    # Initial verify, three persist confirmations per stage, final confirm,
    # and before/after each candidate/checkpoint transport exchange.
    lease = 3 * stage_count + 2 + 2 * (candidate + checkpoint)
    # Initial snapshot, reserve, RUNNING, stage admit/debit, each transport
    # admission, and final settlement. Verified revisions eliminate extra GETs.
    budget = 4 + 2 * stage_count + candidate + checkpoint
    normal = dict(lease=lease, budget=budget, candidate=candidate, checkpoint=checkpoint)
    unknown = sorted(key for key, value in faults.items() if value is None)
    upper = None
    if not unknown:
        extra_effects = checkpoint_readbacks + candidate_reconciliations
        upper = dict(lease=lease + 2 * extra_effects + renewal_requests,
            budget=budget + extra_effects + 2 * revision_conflicts + failure_settlements,
            candidate=candidate + candidate_reconciliations,
            checkpoint=checkpoint + checkpoint_readbacks)
    limits = dict(lease=32, budget=32, candidate=40, checkpoint=12)
    exceeded = sorted(key for key in normal if normal[key] > limits[key])
    return {"schemaVersion": 1, "kind": "fresh_ordinary_v2_candidate_request_envelope",
        "normalRequests": normal, "faultRequestsUpper": faults, "requestsUpper": upper,
        "normalWorkerRequests": sum(normal.values()),
        "workerRequestsUpper": None if upper is None else sum(upper.values()),
        "legacyLimits": limits, "normalExceededClients": exceeded,
        "unknownComponents": unknown, "liveExecutionAllowed": False,
        "providerRows": "UNKNOWN", "accountRemaining": "UNKNOWN",
        "scope": "fresh_core_candidate_with_bundle_and_handoff_exports_only"}


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
