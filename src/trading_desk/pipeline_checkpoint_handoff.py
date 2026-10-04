"""Bounded immutable checkpoint locators, never execution permission or latest pointer."""
from __future__ import annotations

import hashlib
import json
import re

from .pipeline_application import APPLICATION_STAGES, canonical_sha256

MAX_HANDOFF_BYTES = 16_384


def _plan(context, stages, inputs):
    if (context.mode != "ordinary" or context.recovery_id is not None
            or not isinstance(stages, (list, tuple)) or not 1 <= len(stages) <= len(APPLICATION_STAGES)
            or len(set(stages)) != len(stages) or set(stages) - set(APPLICATION_STAGES)
            or not isinstance(inputs, dict) or set(inputs) != set(stages)
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in inputs.values())):
        raise ValueError("checkpoint_handoff_plan_invalid")
    return {"identity": context.identity.as_dict(), "dependencyLockSha256": context.dependency_lock_sha256,
            "mode": context.mode, "stages": list(stages), "stageInputs": inputs}


def handoff_input_sha256(context, stages, inputs):
    return canonical_sha256(_plan(context, stages, inputs))


def _key(context, stage, input_sha, digest):
    run_hash = hashlib.sha256(context.identity.run_key.encode()).hexdigest()
    return f"market-data/pipeline/checkpoints/v1/{run_hash}/{context.identity.producer_build_sha}/{stage}/{input_sha}/{digest}.json"


def build_checkpoint_handoff(context, stages, inputs, exports):
    plan = _plan(context, stages, inputs)
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
                              input_sha256=canonical_sha256(plan))
    return data, digest


def verify_checkpoint_handoff(data, *, sha256, context, input_sha256):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_HANDOFF_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("checkpoint_handoff_digest_or_limit")
    try:
        value = json.loads(data)
        if (not isinstance(value, dict) or set(value) != {"schemaVersion", "kind", "plan", "references"}
                or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                or value["kind"] != "checkpoint_handoff" or not isinstance(value["plan"], dict)):
            raise ValueError()
        plan = value["plan"]
        expected = _plan(context, plan["stages"], plan["stageInputs"])
        if plan != expected or canonical_sha256(expected) != input_sha256:
            raise ValueError()
        references = value["references"]
        if not isinstance(references, dict) or not references or set(references) != set(plan["stages"][:len(references)]):
            raise ValueError()
        for stage, ref in references.items():
            if (not isinstance(ref, dict) or set(ref) != {"key", "sha256", "bytes"}
                    or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])
                    or type(ref["bytes"]) is not int or not 0 < ref["bytes"] <= 1024 * 1024
                    or ref["key"] != _key(context, stage, plan["stageInputs"][stage], ref["sha256"])):
                raise ValueError()
        return {stage: references[stage] for stage in plan["stages"] if stage in references}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("checkpoint_handoff_contract_mismatch") from exc
