"""Pure checkpoint lifecycle evidence checks; never installs policy or deletes.

Caller evidence is not authenticated provider telemetry. Passing these checks
only closes local consistency; production policy/restore verification remains.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

CHECKPOINT_PREFIX = "market-data/pipeline/checkpoints/v1/"
MAX_RULES = 32


def checkpoint_lifecycle_preflight(evidence: dict[str, Any] | None, *, now: datetime) -> dict:
    blockers: list[str] = []
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("lifecycle_timezone_required")
    def block(code: str) -> None:
        blockers.append(code)
    def fresh(value: Any, code: str) -> bool:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            age = (now - timestamp).total_seconds()
            if timestamp.tzinfo is None or not 0 <= age <= ttl:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            block(code)
            return False
        return True
    if not isinstance(evidence, dict):
        block("lifecycle_evidence_unknown")
        evidence = {}
    ttl = evidence.get("evidenceTtlSeconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 86400:
        block("lifecycle_freshness_invalid")
        ttl = 0
    if evidence.get("schemaVersion") != 1 or isinstance(evidence.get("schemaVersion"), bool):
        block("lifecycle_contract_invalid")
    if evidence.get("prefix") != CHECKPOINT_PREFIX:
        block("lifecycle_prefix_mismatch")
    fresh(evidence.get("observedAt"), "lifecycle_observation_stale_or_unknown")
    if evidence.get("bucketLockAbsent") is not True:
        block("lifecycle_bucket_lock_unknown_or_present")
    rules = evidence.get("expirationRules")
    days = None
    if evidence.get("expirationRulesComplete") is not True or not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        block("lifecycle_rules_unknown_or_unbounded")
    else:
        matching = []
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("prefix"), str):
                block("lifecycle_rule_invalid")
                continue
            prefix = rule["prefix"]
            # Any parent/child rule could override the dedicated checkpoint rule.
            if CHECKPOINT_PREFIX.startswith(prefix) or prefix.startswith(CHECKPOINT_PREFIX):
                matching.append(rule)
        if len(matching) != 1 or matching[0]["prefix"] != CHECKPOINT_PREFIX:
            block("lifecycle_overlapping_or_missing_rule")
        else:
            days = matching[0].get("expirationDays")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
                block("lifecycle_retention_invalid")
                days = None
    resume = evidence.get("maximumResumeAgeDays")
    if isinstance(resume, bool) or not isinstance(resume, int) or not 1 <= resume <= 89:
        block("lifecycle_resume_window_invalid")
    elif days is None or resume >= days:
        block("lifecycle_no_resume_retention_margin")
    # Requires complete reference evidence, not a zero-count guess. This candidate
    # policy cannot delete old objects while any reference could expire with them.
    refs = evidence.get("referenceAudit")
    if not isinstance(refs, dict) or refs.get("complete") is not True or refs.get("prefix") != CHECKPOINT_PREFIX:
        block("lifecycle_reference_audit_unknown")
    else:
        fresh(refs.get("observedAt"), "lifecycle_reference_audit_stale")
        if type(refs.get("atRiskCount")) is not int or refs["atRiskCount"] != 0:
            block("lifecycle_active_reference_at_risk")
    restore = evidence.get("restoreEvidence")
    if not isinstance(restore, dict) or restore.get("prefix") != CHECKPOINT_PREFIX or restore.get("verified") is not True:
        block("lifecycle_restore_unknown")
    else:
        fresh(restore.get("observedAt"), "lifecycle_restore_stale")
    blockers = sorted(set(blockers))
    return {"schemaVersion": 1, "kind": "offline_checkpoint_lifecycle_preflight",
            "status": "BLOCKED" if blockers else "LOCAL_POLICY_INPUTS_CHECKED",
            "prefix": CHECKPOINT_PREFIX, "expirationDays": days,
            "blockers": blockers, "productionVerified": False,
            "liveExecutionAllowed": False, "policyMutationAllowed": False,
            "sideEffects": {"remoteReads": False, "remoteWrites": False, "deletion": False}}
