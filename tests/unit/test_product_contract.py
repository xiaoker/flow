from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from trading_desk.product_contract import (
    CONTRACT_VERSION,
    canonical_contract_sha256,
    evaluate_release_constraints,
    load_product_contract,
    validate_product_contract,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "config/flow-product-contract.json"
SCHEMA_PATH = ROOT / "schemas/flow-product-contract.schema.json"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _all_ready_statuses(contract: dict) -> dict[str, str]:
    return {item["id"]: "READY" for item in contract["capabilities"]}


def test_checked_in_contract_matches_json_schema_and_semantics() -> None:
    contract = load_product_contract(CONTRACT_PATH)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(contract))
    assert not errors, [error.message for error in errors]
    assert contract["contractVersion"] == CONTRACT_VERSION
    validate_product_contract(contract)


def test_contract_hash_is_stable_when_json_object_order_changes() -> None:
    contract = _contract()
    reordered = {key: contract[key] for key in reversed(list(contract))}
    assert canonical_contract_sha256(contract) == canonical_contract_sha256(reordered)


def test_state_machines_are_closed_and_have_explicit_reason_flags() -> None:
    contract = _contract()
    for machine in contract["stateDefinitions"].values():
        states = set(machine["states"])
        assert set(machine["terminalStates"]).issubset(states)
        assert machine["transitions"]
        for transition in machine["transitions"]:
            assert transition["from"] in states
            assert transition["to"] in states
            assert isinstance(transition["reasonRequired"], bool)


def test_capability_classification_and_slots_are_explicit() -> None:
    contract = _contract()
    capabilities = {item["id"]: item for item in contract["capabilities"]}
    assert set(capabilities) == {
        "dashboard",
        "etf_radar",
        "etf_periods",
        "national_team",
        "connect",
        "margin",
        "rotation",
        "etf_penetration",
        "stock_search",
        "stock_detail",
    }
    assert {
        key for key, value in capabilities.items() if value["classification"] == "REQUIRED_FOR_DECISION"
    } == {
        "dashboard",
        "etf_radar",
        "etf_periods",
        "national_team",
        "connect",
        "margin",
        "rotation",
    }
    assert all(value["slot"] == "dashboard" for value in capabilities.values() if value["classification"] == "REQUIRED_FOR_DECISION")
    assert all(value["slot"] == "etf_penetration" for value in capabilities.values() if value["classification"] == "OPTIONAL_EXPLICIT")


def test_unknown_required_capability_fails_closed() -> None:
    contract = _contract()
    statuses = _all_ready_statuses(contract)
    statuses["etf_radar"] = "UNKNOWN"
    result = evaluate_release_constraints(
        contract,
        capability_statuses=statuses,
        budget_status="allow",
        as_of_values=["2026-09-13"],
    )
    assert result["eligible"] is False
    assert result["outcome"] == "BLOCKED"
    assert "required_capability:etf_radar:UNKNOWN" in result["blockers"]


def test_unknown_budget_fails_closed_even_when_capabilities_are_ready() -> None:
    contract = _contract()
    result = evaluate_release_constraints(
        contract,
        capability_statuses=_all_ready_statuses(contract),
        budget_status="unknown",
        as_of_values=["2026-09-13"],
    )
    assert result["eligible"] is False
    assert result["outcome"] == "BLOCKED"
    assert result["blockers"] == ["budget:unknown"]


def test_dashboard_visibility_does_not_mean_full_penetration() -> None:
    contract = _contract()
    statuses = _all_ready_statuses(contract)
    statuses["etf_penetration"] = "UNKNOWN"
    result = evaluate_release_constraints(
        contract,
        capability_statuses=statuses,
        budget_status="allow",
        as_of_values=["2026-09-13"],
    )
    assert result["eligible"] is True
    assert result["fullProduct"] is False
    assert "optional_capability:etf_penetration:UNKNOWN" in result["warnings"]


def test_multiple_as_of_values_are_blocked() -> None:
    contract = _contract()
    result = evaluate_release_constraints(
        contract,
        capability_statuses=_all_ready_statuses(contract),
        budget_status="allow",
        as_of_values=["2026-09-12", "2026-09-13"],
    )
    assert result["eligible"] is False
    assert "multiple_as_of_values" in result["blockers"]


def test_v04_contract_is_side_effect_free_and_does_not_touch_protected_paths() -> None:
    contract = _contract()
    assert contract["activation"] == {
        "productionReadPathChanged": False,
        "featureFlagsChanged": False,
        "remoteWrites": False,
        "workflowDispatch": False,
        "activePointerChanged": False,
    }
    source = (ROOT / "src/trading_desk/product_contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_imports = {"__future__", "hashlib", "json", "pathlib", "typing"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported <= allowed_imports
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"write_text", "unlink", "rename", "replace"}
        for node in ast.walk(tree)
    )


def test_contract_validator_script_is_read_only_and_reports_hash() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/validate_flow_product_contract.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    output = json.loads(result.stdout)
    assert output["contractVersion"] == CONTRACT_VERSION
    assert len(output["canonicalSha256"]) == 64
