"""Pure exact recovery scope binding, not live approval or execution permission."""
from datetime import datetime, timezone

from .recovery_execution import validate_recovery_plan


def bind_recovery_runner_scope(plan, context, stages, *, now=None):
    """Only identical stage semantics may be delegated by this first binding.

    dashboard is a product domain, not permission to execute every build
    adapter. Domain-specific mappings require their own proven adapter contract.
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
            or any(stage not in {"core_collection", "publish"} for stage in stages)):
        raise ValueError("recovery_scope_stage_mapping_unproven")
    if any(stage not in scope.required_stages for stage in stages):
        raise ValueError("recovery_scope_stage_not_approved")
    return {"recoveryId": scope.recovery_id, "planSha256": scope.plan_hash,
            "targetDate": context.identity.target_date, "stages": list(stages),
            "liveAuthorityVerified": False}
