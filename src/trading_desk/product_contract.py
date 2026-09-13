"""Pure v0.4 product/release contract validation.

The product contract is deliberately a data-only boundary.  This module
loads and validates the checked-in contract, computes its canonical hash, and
evaluates release constraints without talking to D1, R2, Cloudflare, GitHub,
or an official source.  It is not imported by the production request path;
the later versions may use these definitions only after their own migration
and rollout gates have been approved.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


CONTRACT_SCHEMA_VERSION = 1
CONTRACT_VERSION = "flow-product-v0.4"
CLASSIFICATION_STATUS = "PROPOSED_PENDING_USER_CONFIRMATION"
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "config/flow-product-contract.json"
REQUIRED_STATE_MACHINES = ("source", "marketDay", "decisionRelease")
REQUIRED_FIELDS = (
    "windowDate",
    "dataDate",
    "asOf",
    "observedAt",
    "sourceDates",
    "releaseId",
    "sourceFingerprint",
)
CAPABILITY_CLASSIFICATIONS = {
    "REQUIRED_FOR_DECISION",
    "OPTIONAL_EXPLICIT",
    "NOT_APPLICABLE",
}
READ_MODEL_SLOTS = {"dashboard", "etf_penetration"}
ALLOWED_CAPABILITY_STATES = {
    "READY",
    "UNKNOWN",
    "MISSING",
    "BLOCKED",
    "SOURCE_UNAVAILABLE",
    "NOT_APPLICABLE",
}
ALLOWED_BUDGET_STATES = {"allow", "unknown", "over_budget"}


class ProductContractError(ValueError):
    """Raised when the v0.4 contract violates a semantic invariant."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_contract_sha256(contract: Mapping[str, Any]) -> str:
    """Return a stable digest for the semantic contract content."""

    return hashlib.sha256(_canonical_bytes(contract)).hexdigest()


def load_product_contract(path: str | Path | None = None) -> dict[str, Any]:
    contract_path = Path(path) if path is not None else CONTRACT_PATH
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProductContractError("contract_file_missing", str(contract_path)) from exc
    except json.JSONDecodeError as exc:
        raise ProductContractError("contract_json_invalid", str(contract_path)) from exc
    if not isinstance(payload, dict):
        raise ProductContractError("contract_object_required")
    validate_product_contract(payload)
    return payload


def validate_product_contract(contract: Mapping[str, Any]) -> None:
    """Validate semantic invariants not expressed by the JSON schema."""

    if not isinstance(contract, Mapping):
        raise ProductContractError("contract_object_required")
    if contract.get("schemaVersion") != CONTRACT_SCHEMA_VERSION:
        raise ProductContractError("unsupported_contract_schema")
    if contract.get("contractVersion") != CONTRACT_VERSION:
        raise ProductContractError("unsupported_contract_version")
    if contract.get("classificationStatus") != CLASSIFICATION_STATUS:
        raise ProductContractError("classification_status_must_remain_proposed")
    if contract.get("timezone") != "Asia/Shanghai":
        raise ProductContractError("business_timezone_invalid")
    if contract.get("resourceBudgetRef") != "config/resource-budget.json":
        raise ProductContractError("resource_budget_reference_invalid")

    activation = contract.get("activation")
    if not isinstance(activation, Mapping):
        raise ProductContractError("activation_required")
    for field in (
        "productionReadPathChanged",
        "featureFlagsChanged",
        "remoteWrites",
        "workflowDispatch",
        "activePointerChanged",
    ):
        if activation.get(field) is not False:
            raise ProductContractError("v04_must_be_side_effect_free", field)

    _validate_state_machines(contract.get("stateDefinitions"))
    capabilities = _validate_capabilities(contract.get("capabilities"))
    _validate_slo(contract.get("slo"))
    _validate_field_semantics(contract.get("fieldSemantics"))
    _validate_unique_ids(contract.get("prohibitedCombinations"), "prohibited_combination")
    _validate_unique_ids(contract.get("contractMappings"), "contract_mapping")
    _validate_retirement_candidates(contract.get("retirementCandidates"))

    required = {
        item["id"]
        for item in capabilities
        if item["classification"] == "REQUIRED_FOR_DECISION"
    }
    expected_required = {
        "dashboard",
        "etf_radar",
        "etf_periods",
        "national_team",
        "connect",
        "margin",
        "rotation",
    }
    if required != expected_required:
        raise ProductContractError("required_capability_set_invalid")


def _validate_state_machines(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != set(REQUIRED_STATE_MACHINES):
        raise ProductContractError("state_machine_set_invalid")
    for machine_name in REQUIRED_STATE_MACHINES:
        machine = value[machine_name]
        if not isinstance(machine, Mapping):
            raise ProductContractError("state_machine_object_required", machine_name)
        states = machine.get("states")
        terminal = machine.get("terminalStates")
        transitions = machine.get("transitions")
        if not isinstance(states, list) or not states or len(set(states)) != len(states):
            raise ProductContractError("state_set_invalid", machine_name)
        if not isinstance(terminal, list) or not set(terminal).issubset(states):
            raise ProductContractError("terminal_state_set_invalid", machine_name)
        if not isinstance(transitions, list):
            raise ProductContractError("transition_set_invalid", machine_name)
        seen: set[tuple[str, str]] = set()
        for transition in transitions:
            if not isinstance(transition, Mapping):
                raise ProductContractError("transition_object_required", machine_name)
            source = transition.get("from")
            target = transition.get("to")
            if source not in states or target not in states:
                raise ProductContractError("transition_state_unknown", machine_name)
            if not isinstance(transition.get("reasonRequired"), bool):
                raise ProductContractError("transition_reason_flag_invalid", machine_name)
            pair = (source, target)
            if pair in seen:
                raise ProductContractError("duplicate_transition", f"{machine_name}:{source}->{target}")
            seen.add(pair)


def _validate_capabilities(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ProductContractError("capabilities_required")
    ids: set[str] = set()
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ProductContractError("capability_object_required")
        capability_id = item.get("id")
        if not isinstance(capability_id, str) or not capability_id or capability_id in ids:
            raise ProductContractError("capability_id_invalid_or_duplicate")
        ids.add(capability_id)
        if item.get("classification") not in CAPABILITY_CLASSIFICATIONS:
            raise ProductContractError("capability_classification_invalid", capability_id)
        if item.get("slot") not in READ_MODEL_SLOTS:
            raise ProductContractError("capability_slot_invalid", capability_id)
        source_domains = item.get("sourceDomains")
        if not isinstance(source_domains, list) or not source_domains or len(set(source_domains)) != len(source_domains):
            raise ProductContractError("capability_source_domains_invalid", capability_id)
        if not isinstance(item.get("failurePolicy"), str) or not item["failurePolicy"]:
            raise ProductContractError("capability_failure_policy_invalid", capability_id)
        if not isinstance(item.get("userSurface"), str) or not item["userSurface"]:
            raise ProductContractError("capability_user_surface_invalid", capability_id)
        result.append(item)
    return result


def _validate_slo(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ProductContractError("slo_required")
    if value.get("normalTargetTimezone") != "Asia/Shanghai":
        raise ProductContractError("slo_timezone_invalid")
    if value.get("dailyBudgetSource") != "config/resource-budget.json":
        raise ProductContractError("slo_budget_source_invalid")
    targets = value.get("dailyBudgetTargets")
    expected_targets = {
        "d1RowsReadPerDay": "config/resource-budget.json#/targets/d1RowsReadPerDay",
        "d1RowsWrittenPerDay": "config/resource-budget.json#/targets/d1RowsWrittenPerDay",
        "workersRequestsPerDay": "config/resource-budget.json#/targets/workersRequestsPerDay",
        "githubActionsMinutesPerMonth": "config/resource-budget.json#/targets/githubActionsMinutesPerMonth",
    }
    if targets != expected_targets:
        raise ProductContractError("slo_budget_targets_must_reference_canonical_config")
    for field in (
        "latestCompleteDecisionWithinMinutesAfterRequiredSources",
        "consecutiveTradingDaysWithoutManualRecovery",
    ):
        value_for_field = value.get(field)
        if isinstance(value_for_field, bool) or not isinstance(value_for_field, int) or value_for_field <= 0:
            raise ProductContractError("slo_positive_integer_invalid", field)
    for field in ("maxDuplicateHeavyRunsPerRunKey", "maxActivePointerRollback", "maxCrossDatePageCompositions", "maxWorker1102"):
        if value.get(field) != 0:
            raise ProductContractError("slo_zero_tolerance_invalid", field)


def _validate_field_semantics(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ProductContractError("field_semantics_required")
    for field in REQUIRED_FIELDS:
        definition = value.get(field)
        if not isinstance(definition, Mapping):
            raise ProductContractError("required_field_semantics_missing", field)
        if not isinstance(definition.get("type"), str) or not definition["type"]:
            raise ProductContractError("field_semantic_type_invalid", field)
        if definition.get("timezone") not in {"Asia/Shanghai", "UTC", "source_defined", None}:
            raise ProductContractError("field_semantic_timezone_invalid", field)
        if not isinstance(definition.get("meaning"), str) or not definition["meaning"]:
            raise ProductContractError("field_semantic_meaning_invalid", field)


def _validate_unique_ids(value: Any, kind: str) -> None:
    if not isinstance(value, list) or not value:
        raise ProductContractError(f"{kind}_list_required")
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise ProductContractError(f"{kind}_id_invalid")
        if item["id"] in ids:
            raise ProductContractError(f"duplicate_{kind}", item["id"])
        ids.add(item["id"])


def _validate_retirement_candidates(value: Any) -> None:
    _validate_unique_ids(value, "retirement_candidate")
    for item in value:
        if item.get("candidateVersion") not in {"v0.9", "v1.0"}:
            raise ProductContractError("retirement_candidate_version_invalid", item["id"])
        proof = item.get("requiredProof")
        if not isinstance(proof, list) or not proof or len(set(proof)) != len(proof):
            raise ProductContractError("retirement_candidate_proof_invalid", item["id"])


def evaluate_release_constraints(
    contract: Mapping[str, Any],
    *,
    capability_statuses: Mapping[str, str],
    budget_status: str,
    as_of_values: Sequence[str],
) -> dict[str, Any]:
    """Evaluate v0.4 constraints without performing a release side effect.

    This is intentionally a pure contract helper for fixtures and future
    builders.  A missing or unknown required fact is never converted into a
    successful release; optional gaps remain visible in ``warnings`` and do
    not claim full-product completeness.
    """

    validate_product_contract(contract)
    blockers: list[str] = []
    warnings: list[str] = []
    capability_by_id = {item["id"]: item for item in contract["capabilities"]}
    for capability_id, definition in capability_by_id.items():
        status = capability_statuses.get(capability_id, "UNKNOWN")
        if status not in ALLOWED_CAPABILITY_STATES:
            status = "UNKNOWN"
        if definition["classification"] == "REQUIRED_FOR_DECISION":
            if status not in {"READY", "NOT_APPLICABLE"}:
                blockers.append(f"required_capability:{capability_id}:{status}")
        elif status not in {"READY", "NOT_APPLICABLE"}:
            warnings.append(f"optional_capability:{capability_id}:{status}")

    if budget_status not in ALLOWED_BUDGET_STATES:
        budget_status = "unknown"
    if budget_status != "allow":
        blockers.append(f"budget:{budget_status}")
    if len(set(as_of_values)) > 1:
        blockers.append("multiple_as_of_values")
    if not as_of_values:
        blockers.append("as_of_missing")

    return {
        "eligible": not blockers,
        "outcome": "ACTIVE_ELIGIBLE" if not blockers else "BLOCKED",
        "fullProduct": not blockers and not warnings,
        "blockers": sorted(blockers),
        "warnings": sorted(warnings),
    }
