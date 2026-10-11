"""Bounded immutable checkpoint locators, never execution permission or latest pointer."""
from __future__ import annotations

import hashlib
import json
import re

from .pipeline_application import APPLICATION_STAGES, canonical_sha256

MAX_HANDOFF_BYTES = 16_384


def _plan(context, stages, inputs, approved_plan=None, now=None):
    if (not isinstance(stages, (list, tuple)) or not 1 <= len(stages) <= len(APPLICATION_STAGES)
            or len(set(stages)) != len(stages) or set(stages) - set(APPLICATION_STAGES)
            or not isinstance(inputs, dict) or set(inputs) != set(stages)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in inputs.values())):
        raise ValueError("checkpoint_handoff_plan_invalid")
    value = {"identity": context.identity.as_dict(), "dependencyLockSha256": context.dependency_lock_sha256,
             "mode": context.mode, "stages": list(stages), "stageInputs": inputs}
    if context.mode == "recovery":
        from .pipeline_recovery_scope import bind_recovery_runner_scope
        from .recovery_execution import plan_bytes
        if approved_plan is None:
            raise ValueError("checkpoint_handoff_plan_invalid")
        bind_recovery_runner_scope(approved_plan, context, stages, now=now)
        value["recovery"] = {"recoveryId": context.recovery_id,
            "planSha256": context.recovery_plan_sha256, "scopeSha256": plan_bytes(approved_plan)[1]}
    elif context.mode != "ordinary" or context.recovery_id is not None or approved_plan is not None:
        raise ValueError("checkpoint_handoff_plan_invalid")
    return value


def handoff_input_sha256(context, stages, inputs, approved_plan=None, now=None):
    return canonical_sha256(_plan(context, stages, inputs, approved_plan, now))


def _key(context, stage, input_sha, digest, recovery=None):
    run_hash = hashlib.sha256(context.identity.run_key.encode()).hexdigest()
    namespace = f'recovery/{recovery["recoveryId"]}/{recovery["scopeSha256"]}/' if recovery else ""
    return f"market-data/pipeline/checkpoints/v1/{namespace}{run_hash}/{context.identity.producer_build_sha}/{stage}/{input_sha}/{digest}.json"


def build_checkpoint_handoff(context, stages, inputs, exports, approved_plan=None, now=None):
    plan = _plan(context, stages, inputs, approved_plan, now)
    references = {}
    for stage in stages:
        if stage not in exports:
            break
        descriptor = exports[stage].get("remote")
        if not isinstance(descriptor, dict):
            raise ValueError("checkpoint_handoff_remote_proof_missing")
        references[stage] = descriptor
    if set(exports) != set(references) or not references:
        raise ValueError("checkpoint_handoff_nonprefix_or_empty")
    value = {"schemaVersion": 1, "kind": "checkpoint_handoff", "plan": plan, "references": references}
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(data).hexdigest()
    verify_checkpoint_handoff(data, sha256=digest, context=context,
                              input_sha256=canonical_sha256(plan), approved_plan=approved_plan, now=now)
    return data, digest


def verify_checkpoint_handoff(data, *, sha256, context, input_sha256, approved_plan=None, now=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_HANDOFF_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("checkpoint_handoff_digest_or_limit")
    try:
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {"schemaVersion", "kind", "plan", "references"}
                or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                or value["kind"] != "checkpoint_handoff" or not isinstance(value["plan"], dict)):
            raise ValueError()
        plan = value["plan"]
        expected = _plan(context, plan["stages"], plan["stageInputs"], approved_plan, now)
        if plan != expected or canonical_sha256(expected) != input_sha256:
            raise ValueError()
        references = value["references"]
        if not isinstance(references, dict) or not references or set(references) != set(plan["stages"][:len(references)]):
            raise ValueError()
        for stage, ref in references.items():
            if (not isinstance(ref, dict) or set(ref) != {"key", "sha256", "bytes"}
                    or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
                    or type(ref["bytes"]) is not int or not 0 < ref["bytes"] <= 1024 * 1024
                    or ref["key"] != _key(context, stage, plan["stageInputs"][stage], ref["sha256"], expected.get("recovery"))):
                raise ValueError()
        return {stage: references[stage] for stage in plan["stages"] if stage in references}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("checkpoint_handoff_contract_mismatch") from exc
