from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.audit_controlled_recovery import main
from trading_desk.recovery_audit import RecoveryAuditError, audit_bundle, write_audit_outputs
from trading_desk.recovery_execution import RecoveryContractError


CONFIG = json.loads(
    (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text(encoding="utf-8")
)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def ready_row(data_date: str, **changes) -> dict:
    row = {
        "dataDate": data_date,
        "domain": "sse_etf",
        "market": "sse",
        "required": True,
        "applicability": "REQUIRED",
        "stageStatus": {
            "raw": "READY",
            "normalized": "READY",
            "model": "READY",
            "pcf_exact": "READY",
            "dashboard": "READY",
            "etf_penetration": "READY",
            "date_visible": "READY",
        },
        "rawKey": f"market-data/{data_date}/sse-etf.csv",
        "rawSha256": SHA_A,
        "normalizedKey": f"market-data/{data_date}/sse-etf.normalized.json",
        "normalizedSha256": SHA_B,
        "hashOrigin": "declared_header",
        "sourceBusinessDate": data_date,
        "publicationDate": data_date,
        "navDate": data_date,
        "pcfDate": data_date,
        "effectiveDate": data_date,
        "rows": 10,
        "unit": "份",
        "schemaVersion": 1,
        "calculationVersion": "flow-v1",
        "sourceFingerprint": SHA_C,
        "hashStatus": "READY",
    }
    row.update(changes)
    return row


def complete_bundle(*, rows: list[dict] | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "campaignId": "r0-test",
        "scope": {
            "startDate": "2026-08-28",
            "endDateInclusive": "2026-08-29",
            "priorityDates": ["2026-08-28"],
        },
        "domains": [{"id": "sse_etf", "market": "sse", "required": True}],
        "inventory": rows if rows is not None else [ready_row("2026-08-28"), ready_row("2026-08-29")],
        "releaseManifests": [
            {"asOf": "2026-08-28", "slot": "dashboard", "complete": True, "releaseId": "dash-28"},
            {"asOf": "2026-08-28", "slot": "etf_penetration", "complete": True, "releaseId": "penetration-28"},
            {"asOf": "2026-08-29", "slot": "dashboard", "complete": True, "releaseId": "dash-29"},
            {"asOf": "2026-08-29", "slot": "etf_penetration", "complete": True, "releaseId": "penetration-29"},
        ],
        "pipelineWindows": [
            {
                "runKey": "daily-market-data:2026-08-28:reconcile:v1",
                "windowDate": "2026-08-28",
                "status": "COMPLETED",
            }
        ],
        "r2Inventory": {
            "page": 0,
            "pageSize": 1,
            "hardLimit": 100,
            "hasMore": False,
            "indexSha256": SHA_C,
            "suspiciousGaps": [],
        },
        "r2Objects": [
            {
                "key": "market-data/2026-08-28/sse-etf.csv",
                "sha256": SHA_A,
                "dataDate": "2026-08-28",
                "domain": "sse_etf",
                "stage": "raw",
                "bytes": 100,
            }
        ],
        "accountTelemetry": {
            "d1RowsReadPerDay": 10,
            "d1RowsWrittenPerDay": 2,
            "workersRequestsPerDay": 1,
            "githubActionsMinutes": 1,
            "r2StorageBytes": 100,
            "r2ClassA": 1,
            "r2ClassB": 1,
        },
        "d1Audit": {"rowsRead": 10, "rowsWritten": 2, "workersRequests": 1},
        "retryAttempts": 1,
    }


def test_plan_hash_is_stable_for_reordered_fixture() -> None:
    first = audit_bundle(complete_bundle(), budget_config=CONFIG)
    second_bundle = complete_bundle()
    second_bundle["inventory"] = list(reversed(second_bundle["inventory"]))
    second_bundle["releaseManifests"] = list(reversed(second_bundle["releaseManifests"]))
    second = audit_bundle(second_bundle, budget_config=CONFIG)
    assert first["plan"]["planHash"] == second["plan"]["planHash"]
    assert len(first["inventory"]) == 2
    assert first["plan"]["status"] == "NOT_APPROVED"
    assert first["plan"]["executionBlocked"] is True


def test_matrix_expands_date_and_domain_and_separates_slots() -> None:
    bundle = complete_bundle(
        rows=[
            ready_row("2026-08-28", stageStatus={"raw": "READY", "normalized": "READY", "model": "READY", "pcf_exact": "READY", "dashboard": "READY", "etf_penetration": "MISSING", "date_visible": "READY"}),
            ready_row("2026-08-29"),
        ]
    )
    bundle["releaseManifests"][1]["complete"] = False
    result = audit_bundle(bundle, budget_config=CONFIG)
    assert result["plan"]["dateSummaries"][0]["stageStatus"]["dashboard"] == "READY"
    assert result["plan"]["dateSummaries"][0]["stageStatus"]["etf_penetration"] == "MISSING"
    assert result["targets"] == [
        {
            "dataDate": "2026-08-28",
            "requiredStages": ["etf_penetration"],
            "reason": ["etf_penetration_missing"],
            "dependsOn": [],
        }
    ]


def test_unknown_applicability_and_budget_fail_closed() -> None:
    bundle = complete_bundle(rows=[])
    bundle["accountTelemetry"] = {}
    bundle["d1Audit"] = {}
    bundle.pop("r2Inventory")
    result = audit_bundle(bundle, budget_config=CONFIG)
    assert len(result["inventory"]) == 2
    assert not result["targets"]
    assert result["budget"]["decision"] == "unknown_metrics"
    assert result["stageEvidence"]["status"] == "DONE_WITH_CONCERNS"
    assert result["stageEvidence"]["sourceEvidenceStatus"] == "INCOMPLETE"
    assert result["stageEvidence"]["nextAllowedAction"] == "R0-D_RECONCILE"


def test_source_unavailable_is_reported_but_official_closed_is_not() -> None:
    rows = [
        ready_row("2026-08-28", applicability="REQUIRED", stageStatus={"raw": "SOURCE_UNAVAILABLE"}),
        ready_row("2026-08-29", applicability="OFFICIAL_CLOSED", stageStatus={"raw": "SOURCE_UNAVAILABLE"}),
    ]
    result = audit_bundle(complete_bundle(rows=rows), budget_config=CONFIG)
    assert {(item["dataDate"], item["stage"]) for item in result["sourceUnavailable"]} == {
        ("2026-08-28", "raw")
    }


def test_missing_hash_is_unknown_and_declared_mismatch_is_blocked() -> None:
    missing = audit_bundle(
        complete_bundle(rows=[ready_row("2026-08-28", rawSha256=None)]), budget_config=CONFIG
    )
    assert missing["inventory"][0]["stages"]["raw"] == "UNKNOWN"
    mismatch = audit_bundle(
        complete_bundle(
            rows=[ready_row("2026-08-28", declaredRawSha256=SHA_A, computedRawSha256=SHA_B)]
        ),
        budget_config=CONFIG,
    )
    assert mismatch["inventory"][0]["hashStatus"] == "BLOCKED"
    assert mismatch["inventory"][0]["stages"]["raw"] == "BLOCKED"
    assert mismatch["stageEvidence"]["status"] == "BLOCKED"


def test_r2_requires_page_metadata_and_hard_limit() -> None:
    no_page = complete_bundle()
    no_page.pop("r2Inventory")
    result = audit_bundle(no_page, budget_config=CONFIG)
    assert result["budget"]["r2InventoryStatus"] == "UNKNOWN"
    too_large = complete_bundle()
    too_large["r2Inventory"]["pageSize"] = 1001
    with pytest.raises(RecoveryAuditError, match="r2_page_size_exceeds_limit"):
        audit_bundle(too_large, budget_config=CONFIG)


@pytest.mark.parametrize(
    "mutator, error",
    [
        (lambda bundle: bundle["scope"].update(startDate="2026-09-01", endDateInclusive="2026-08-01"), "scope_date_order_invalid"),
        (lambda bundle: bundle["inventory"][0].update(rawSha256="not-a-sha"), "invalid_raw_sha256"),
        (lambda bundle: bundle["inventory"].append(dict(bundle["inventory"][0])), "duplicate_inventory_row"),
        (lambda bundle: bundle.update(apiToken="should-not-be-accepted"), "secret_field_rejected"),
        (lambda bundle: bundle["pipelineWindows"][0].update(runKey="not-a-run-key"), "invalid_run_key"),
    ],
)
def test_input_contract_rejects_invalid_or_secret_evidence(mutator, error: str) -> None:
    bundle = complete_bundle()
    mutator(bundle)
    with pytest.raises(RecoveryAuditError, match=error):
        audit_bundle(bundle, budget_config=CONFIG)


def test_outputs_are_bounded_and_protected_paths_are_rejected(tmp_path: Path) -> None:
    result = audit_bundle(complete_bundle(), budget_config=CONFIG)
    output_dir = tmp_path / "audit"
    write_audit_outputs(result, output_dir)
    assert {item.name for item in output_dir.iterdir()} == {
        "inventory.json",
        "inventory.csv",
        "recovery-plan.json",
        "source-unavailable.csv",
        "budget-estimate.json",
        "stage-evidence.json",
        "summary.md",
    }
    with pytest.raises(RecoveryContractError, match="recovery_output_protected_path"):
        write_audit_outputs(result, Path("reports/phase0/latest-quality-summary.json"))


def test_cli_is_offline_and_writes_stage_evidence(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(complete_bundle(), ensure_ascii=False), encoding="utf-8")
    config_path = tmp_path / "budget.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    output_dir = tmp_path / "cli-output"
    assert main(["--input", str(bundle_path), "--config", str(config_path), "--output-dir", str(output_dir)]) == 0
    evidence = json.loads((output_dir / "stage-evidence.json").read_text(encoding="utf-8"))
    assert evidence["phase"] == "R0-C"
    assert evidence["readOnlyAssertions"] == {
        "d1Writes": 0,
        "r2Writes": 0,
        "workflowDispatches": 0,
        "activePointerChanges": 0,
        "flagChanges": 0,
    }


def test_stage_evidence_schema_declares_r0_c_stop_contract() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas/recovery-stage-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["phase"]["const"] == "R0-C"
    assert schema["properties"]["migrationApplied"]["const"] is False
    assert schema["properties"]["protectedPathsTouched"]["const"] == []


def test_auditor_dependency_surface_is_offline_and_pure(monkeypatch) -> None:
    source = (Path(__file__).resolve().parents[2] / "src/trading_desk/recovery_audit.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    forbidden = {"requests", "urllib", "subprocess", "boto3", "cloudflare", "wrangler"}
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert imported.isdisjoint(forbidden)
    # Calling the auditor must not write any output; only the explicit writer
    # is allowed to create the seven local artifacts after the audit returns.
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected write")))
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected mkdir")))
    result = audit_bundle(complete_bundle(), budget_config=CONFIG)
    assert result["stageEvidence"]["readOnlyAssertions"]["d1Writes"] == 0
