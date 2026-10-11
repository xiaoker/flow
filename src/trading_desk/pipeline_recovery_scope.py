"""Pure exact recovery scope binding, not live approval or execution permission."""
from datetime import datetime, timezone

from .recovery_execution import validate_recovery_plan


def bind_recovery_runner_scope(plan, context, stages, *, now=None):
    """Bind approved domains to the existing core stage semantics.

    dashboard maps only to build/verify; the runner must separately enforce
    the exact core adapters. This pure check is not generic adapter authority.
    This function never reads remote state, authorizes a lease or spends budget.
    """
    if context.mode != "recovery" or context.authorization_kind != "approved-recovery":
        raise ValueError("recovery_scope_context_required")
    scope = validate_recovery_plan(plan, expected_plan_hash=context.recovery_plan_sha256)
    if (scope.recovery_id != context.recovery_id or scope.run_key != context.identity.run_key
            or context.identity.target_date not in scope.data_dates):
        raise ValueError("recovery_scope_identity_mismatch")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current >= datetime.fromisoformat(scope.deadline.replace("Z", "+00:00")):
        raise ValueError("recovery_scope_deadline_expired")
    if (not isinstance(stages, (tuple, list)) or not stages or len(stages) != len(set(stages))
            or any(stage not in {"core_collection", "publish", "build", "verify"} for stage in stages)):
        raise ValueError("recovery_scope_stage_mapping_unproven")
    domains = {stage: "dashboard" if stage in {"build", "verify"} else stage for stage in stages}
    if any(domain not in scope.required_stages for domain in domains.values()):
        raise ValueError("recovery_scope_stage_not_approved")
    return {"recoveryId": scope.recovery_id, "planSha256": scope.plan_hash,
            "targetDate": context.identity.target_date, "stages": list(stages),
            "stageDomains": domains,
            "liveAuthorityVerified": False}


def validate_recovery_dashboard_adapters(adapters, lease_guard, plan, context, stages, *, now=None):
    """Only the existing offline dashboard builder and verifier are admitted.

    Neither adapter fetches sources or publishes. Their existing exact input,
    date, coverage, file inventory and recomputation checks remain mandatory.
    """
    from .pipeline_build_adapter import CoreReadModelBuildAdapter
    from .pipeline_verify_adapter import CoreReadModelVerifyAdapter
    from .pipeline_recovery_lease import RecoveryPipelineLease

    bind_recovery_runner_scope(plan, context, stages, now=now)
    expected = {"build": CoreReadModelBuildAdapter, "verify": CoreReadModelVerifyAdapter}
    selected = [stage for stage in stages if stage in expected]
    if (not selected or not isinstance(lease_guard, RecoveryPipelineLease)
            or any(type(adapters.get(stage)) is not expected[stage] for stage in selected)):
        raise ValueError("recovery_dashboard_adapter_binding_required")
    return True


def validate_recovery_publication_adapter(adapter, lease_guard, plan, context, *, now=None):
    """Bind the only proven remote publication adapter; no domain aliases.

    This validates wiring, not live authority. The runner must still verify the
    lease and the transport/server must fence every actual operation.
    """
    from .pipeline_publish_adapter import CoreCandidatePublishAdapter
    from .pipeline_candidate_transport import RecoveryCandidatePublicationTransport
    from .pipeline_recovery_lease import RecoveryPipelineLease
    from .resource_budget_remote import RecoverySharedBudgetLedger

    bind_recovery_runner_scope(plan, context, ("publish",), now=now)
    if (type(adapter) is not CoreCandidatePublishAdapter
            or not isinstance(lease_guard, RecoveryPipelineLease)
            or not isinstance(adapter.transport, RecoveryCandidatePublicationTransport)
            or not isinstance(adapter.transport._ledger, RecoverySharedBudgetLedger)
            or adapter.transport._guard is not lease_guard
            or adapter.transport._ledger._guard is not lease_guard):
        raise ValueError("recovery_publication_adapter_binding_required")
    execution = adapter.transport.execution(context)
    if execution != adapter.transport.execution(adapter.execution_context):
        raise ValueError("recovery_publication_context_mismatch")
    adapter.transport._ledger.validate_execution_identity(
        run_key=execution["runKey"], lease_owner=execution["leaseOwner"],
        attempt_count=execution["attemptCount"], target_date=execution["targetDate"],
        task_id=adapter.transport._task, recovery_id=execution["recoveryId"],
        recovery_plan_sha256=execution["planSha256"])
    return True
