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
        "accountTelemetryEvidence": {
            "status": "VALID",
            "reason": "fixture",
            "capturedAt": "2026-09-07T02:00:00Z",
        },
        "d1Audit": {"rowsRead": 10, "rowsWritten": 0, "workersRequests": 1},
        "resourceUsage": {
            "schemaVersion": 1,
            "metricsMeasuredAt": "2026-09-07T02:00:00Z",
            "actual": {
                "d1RowsRead": 10,
                "d1RowsWritten": 0,
                "workerRequests": 1,
                "r2ClassA": 0,
                "r2ClassB": 1,
                "inventoryPages": 1,
                "responseBytes": 1000,
                "actionsMinutes": "unknown",
            },
        },
        "readOnlyAssertions": {
            "d1Writes": 0,
            "r2Writes": 0,
            "workflowDispatches": 0,
            "activePointerChanges": 0,
            "flagChanges": 0,
        },
        "protectedPathsTouched": [],
        "runtimeEvidence": {
            "metricsMeasuredAt": "2026-09-07T10:00:00+08:00",
            "repository": {"originMain": "2eecc643761bcbcc23c91f25f11510afea854427"},
        },
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


def test_r0d_phase_closes_complete_evidence_without_changing_plan_hash() -> None:
    r0c = audit_bundle(complete_bundle(), budget_config=CONFIG)
    r0d = audit_bundle(complete_bundle(), budget_config=CONFIG, phase="R0-D")
    assert r0c["plan"]["planHash"] == r0d["plan"]["planHash"]
    assert r0c["stageEvidence"]["phase"] == "R0-C"
    assert r0c["stageEvidence"]["reconciliationStatus"] == "NOT_APPLICABLE"
    assert r0c["stageEvidence"]["nextAllowedAction"] == "R0-D_RECONCILE"
    assert r0d["stageEvidence"]["phase"] == "R0-D"
    assert r0d["stageEvidence"]["status"] == "DONE"
    assert r0d["stageEvidence"]["reconciliationStatus"] == "R0_RECONCILED"
    assert r0d["stageEvidence"]["nextAllowedAction"] == "R2_ACCEPTANCE_AUDIT"
    assert r0d["stageEvidence"]["budgetEvidenceMode"] == "ACCOUNT_VERIFIED"
    assert r0d["stageEvidence"]["localBudgetStatus"] == "allow"
    assert r0d["summary"].startswith("# Controlled recovery R0-D audit")


def test_r0d_closes_on_bounded_local_budget_without_official_account_snapshot() -> None:
    bundle = complete_bundle()
    verified = audit_bundle(complete_bundle(), budget_config=CONFIG, phase="R0-D")
    bundle["accountTelemetry"] = {}
    bundle["accountTelemetryEvidence"] = {
        "status": "UNKNOWN",
        "reason": "not_provided",
    }
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-D")
    assert result["budget"]["decision"] == "allow"
    assert result["budget"]["evidenceMode"] == "LOCAL_BOUNDED"
    assert result["budget"]["accountDecision"] == "unknown_metrics"
    assert result["budget"]["unknownMetrics"] == []
    assert "githubActionsMinutes" in result["budget"]["accountUnknownMetrics"]
    assert result["stageEvidence"]["status"] == "DONE"
    assert result["stageEvidence"]["unknowns"] == []
    assert result["stageEvidence"]["reconciliationStatus"] == "R0_RECONCILED"
    assert result["stageEvidence"]["nextAllowedAction"] == "R2_ACCEPTANCE_AUDIT"
    assert result["plan"]["planHash"] == verified["plan"]["planHash"]
    assert "Recovery and backfill remain subject" in result["summary"]
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas/recovery-stage-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = list(Draft202012Validator(schema).iter_errors(result["stageEvidence"]))
    assert not errors, [error.message for error in errors]


def test_r0c_does_not_reclassify_missing_account_snapshot_as_local_bounded() -> None:
    bundle = complete_bundle()
    bundle["accountTelemetry"] = {}
    bundle["accountTelemetryEvidence"] = {"status": "UNKNOWN", "reason": "not_provided"}
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-C")
    assert result["budget"]["decision"] == "unknown_metrics"
    assert result["budget"]["evidenceMode"] == "UNVERIFIED"
    assert result["stageEvidence"]["status"] == "DONE_WITH_CONCERNS"


def test_r0d_local_budget_unknown_or_exceeded_fails_closed() -> None:
    unknown = complete_bundle()
    unknown["accountTelemetry"] = {}
    unknown["accountTelemetryEvidence"] = {"status": "UNKNOWN", "reason": "not_provided"}
    unknown["resourceUsage"]["actual"]["r2ClassB"] = "unknown"
    unknown_result = audit_bundle(unknown, budget_config=CONFIG, phase="R0-D")
    assert unknown_result["budget"]["decision"] == "unknown_metrics"
    assert unknown_result["stageEvidence"]["reconciliationStatus"] == "PENDING"
    assert "local.r2ClassB" in unknown_result["stageEvidence"]["unknowns"]

    exceeded = complete_bundle()
    exceeded["accountTelemetry"] = {}
    exceeded["accountTelemetryEvidence"] = {"status": "UNKNOWN", "reason": "not_provided"}
    exceeded["resourceUsage"]["actual"]["workerRequests"] = 301
    exceeded["d1Audit"]["workersRequests"] = 301
    exceeded_result = audit_bundle(exceeded, budget_config=CONFIG, phase="R0-D")
    assert exceeded_result["budget"]["decision"] == "over_budget"
    assert exceeded_result["stageEvidence"]["status"] == "BLOCKED"
    assert exceeded_result["stageEvidence"]["nextAllowedAction"] == "STOP"
    assert "local.workerRequests:over_limit" in exceeded_result["stageEvidence"]["blockers"]


def test_r0d_local_counter_mismatch_is_blocked() -> None:
    bundle = complete_bundle()
    bundle["resourceUsage"]["actual"]["d1RowsRead"] = 11
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-D")
    assert result["budget"]["decision"] == "over_budget"
    assert "local.d1RowsRead:counter_mismatch" in result["stageEvidence"]["blockers"]


def test_unknown_phase_is_rejected() -> None:
    with pytest.raises(RecoveryAuditError, match="invalid_audit_phase"):
        audit_bundle(complete_bundle(), budget_config=CONFIG, phase="R1")


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
    assert result["sourceUnavailable"][0]["provenanceStatus"] == "INCOMPLETE"


def test_evidenced_source_unavailable_is_a_known_closed_state() -> None:
    row = ready_row(
        "2026-08-28",
        stageStatus={
            "raw": "SOURCE_UNAVAILABLE",
            "normalized": "SOURCE_UNAVAILABLE",
            "model": "SOURCE_UNAVAILABLE",
            "pcf_exact": "SOURCE_UNAVAILABLE",
        },
        gapReason="official source did not publish this dataset",
        evidence={"marketHistory": True},
    )
    result = audit_bundle(
        complete_bundle(rows=[row, ready_row("2026-08-29")]),
        budget_config=CONFIG,
        phase="R0-D",
    )
    assert result["sourceUnavailable"][0]["provenanceStatus"] == "COMPLETE"
    assert result["stageEvidence"]["sourceEvidenceStatus"] == "READY_WITH_EVIDENCED_UNAVAILABLE"
    assert result["stageEvidence"]["reconciliationStatus"] == "R0_RECONCILED"
    assert result["stageEvidence"]["nextAllowedAction"] == "R2_ACCEPTANCE_AUDIT"


def test_r0d_missing_runtime_safety_evidence_cannot_close() -> None:
    bundle = complete_bundle()
    bundle.pop("readOnlyAssertions")
    bundle.pop("protectedPathsTouched")
    bundle["runtimeEvidence"] = {"repository": {"originMain": "2eecc643761bcbcc23c91f25f11510afea854427"}}
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-D")
    assert result["stageEvidence"]["status"] == "DONE_WITH_CONCERNS"
    assert result["stageEvidence"]["reconciliationStatus"] == "PENDING"
    assert result["stageEvidence"]["nextAllowedAction"] == "R0-D_RECONCILE"
    assert "readOnlyAssertions:UNKNOWN" in result["stageEvidence"]["unknowns"]
    assert "metricsMeasuredAt:UNKNOWN" in result["stageEvidence"]["unknowns"]


def test_r0d_nonzero_read_only_assertion_blocks() -> None:
    bundle = complete_bundle()
    bundle["readOnlyAssertions"]["d1Writes"] = 1
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-D")
    assert result["stageEvidence"]["status"] == "BLOCKED"
    assert result["stageEvidence"]["nextAllowedAction"] == "STOP"
    assert "readOnlyAssertions:d1Writes=1" in result["stageEvidence"]["blockers"]


def test_dashboard_visibility_cannot_close_missing_penetration_slot() -> None:
    bundle = complete_bundle()
    bundle["releaseManifests"] = [
        item for item in bundle["releaseManifests"] if item["slot"] == "dashboard"
    ]
    for row in bundle["inventory"]:
        row["stageStatus"]["etf_penetration"] = "MISSING"
    result = audit_bundle(bundle, budget_config=CONFIG, phase="R0-D")
    assert result["stageEvidence"]["reconciliationStatus"] == "R0_RECONCILED"
    assert result["stageEvidence"]["nextAllowedAction"] == "R2_ACCEPTANCE_AUDIT"
    assert result["stageEvidence"]["status"] == "DONE"
    assert result["targets"][0]["requiredStages"] == ["etf_penetration"]


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
    too_large["r2Inventory"]["pageSize"] = 20_001
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
    r0d_output_dir = tmp_path / "cli-r0d-output"
    assert (
        main(
            [
                "--input",
                str(bundle_path),
                "--config",
                str(config_path),
                "--phase",
                "R0-D",
                "--output-dir",
                str(r0d_output_dir),
            ]
        )
        == 0
    )
    r0d_evidence = json.loads((r0d_output_dir / "stage-evidence.json").read_text(encoding="utf-8"))
    assert r0d_evidence["phase"] == "R0-D"
    assert r0d_evidence["planHash"] == evidence["planHash"]


def test_stage_evidence_schema_declares_r0_c_stop_contract() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas/recovery-stage-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(schema["properties"]["phase"]["enum"]) == {"R0-C", "R0-D"}
    assert schema["properties"]["schemaVersion"]["const"] == 2
    assert schema["properties"]["contractVersion"]["const"] == "recovery-audit-v2"
    assert schema["properties"]["migrationApplied"]["const"] is False
    assert schema["properties"]["nextAllowedAction"]["enum"] == [
        "STOP",
        "R0-D_RECONCILE",
        "R2_ACCEPTANCE_AUDIT",
    ]
    assert "reconciliationStatus" in schema["required"]
    assert "budgetEvidenceMode" in schema["required"]
    assert set(schema["properties"]["budgetEvidenceMode"]["enum"]) == {
        "ACCOUNT_VERIFIED",
        "LOCAL_BOUNDED",
        "UNVERIFIED",
    }


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
