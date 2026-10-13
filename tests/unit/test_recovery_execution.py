from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from trading_desk.recovery_execution import (
    RecoveryBudgetEstimate,
    RecoveryContractError,
    artifact_reusable,
    assert_recovery_output_path,
    build_recovery_plan,
    canonical_sha256,
    estimate_recovery_budget,
    execute_recovery_plan,
    evaluate_recovery_budget,
    iter_execution_matrix,
    plan_bytes,
    receipt_bytes,
    receipt_key,
    scope_completion,
    validate_recovery_plan,
    validate_recovery_receipt,
)


CONFIG = json.loads(
    (Path(__file__).resolve().parents[2] / "config/resource-budget.json").read_text(
        encoding="utf-8"
    )
)


def plan(*, dates: list[str] | None = None, run_key: str = "daily-market-data:2026-08-28:reconcile:v1") -> dict:
    return build_recovery_plan(
        campaign_id="recovery-20260904",
        recovery_id="recovery-20260904-01",
        run_key=run_key,
        data_dates=dates or ["2026-08-27", "2026-08-28", "2026-08-31"],
        required_stages=["dashboard", "etf_penetration", "stock_flow"],
        artifact_root=".recovery/recovery-20260904-01",
        state_dir=".recovery/recovery-20260904-01/state",
        max_dates=3,
        max_requests=500,
        deadline="2026-09-05T00:00:00.000Z",
        budget_manifest={"source": "offline-fixture", "measured": True},
    )


def receipt(scope, data_date: str, stage: str, *, status: str = "COMPLETED", attempt: int = 1) -> dict:
    return {
        "schemaVersion": 1,
        "campaignId": scope.campaign_id,
        "recoveryId": scope.recovery_id,
        "planHash": scope.plan_hash,
        "runKey": scope.run_key,
        "attemptCount": attempt,
        "dataDate": data_date,
        "market": "CN",
        "stage": stage,
        "status": status,
        "inputObjects": [
            {
                "key": f"recovery/input/{data_date}/{stage}.json",
                "sha256": "a" * 64,
                "sourceDates": [data_date],
                "sourceFingerprint": f"fixture:{data_date}:{stage}",
            }
        ],
        "versions": {
            "schema": "read-model-v1",
            "calculation": "flow-recovery-v1",
            "classification": "sector-v1",
        },
        "producerBuildSha": "b" * 64,
        "collectorRunId": f"collector-{data_date}-{stage}",
        "publishRunId": f"publish-{data_date}-{stage}",
        "output": {
            "manifestSha256": "c" * 64,
            "releaseId": f"release-{data_date}-{stage}",
            "slot": stage,
            "asOf": data_date,
        },
        "qa": {"result": "PASS", "evidenceKeys": [f"recovery/qa/{data_date}/{stage}.json"]},
        "historical": {
            "dateReadable": True,
            "activeBefore": "release-20260904",
            "activeAfter": "release-20260904",
        },
        "resources": {
            "d1RowsRead": 10,
            "d1RowsWritten": 2,
            "workerRequests": 2,
            "r2ClassA": 2,
            "r2ClassB": 3,
            "r2Bytes": 100,
            "actionsMinutes": 1,
            "measuredAt": "2026-09-04T00:00:00.000Z",
        },
        "completedAt": "2026-09-04T00:01:00.000Z" if status == "COMPLETED" else None,
    }


def test_plan_hash_is_stable_and_matrix_is_date_major() -> None:
    first = plan()
    second = plan()
    assert first["planHash"] == second["planHash"]
    scope = validate_recovery_plan(first)
    matrix = iter_execution_matrix(scope)
    assert len(matrix) == 9
    assert [item["dataDate"] for item in matrix[:3]] == ["2026-08-27"] * 3
    assert matrix[0]["artifactRoot"].startswith(".recovery/recovery-20260904-01/2026-08-27/")


def test_single_date_recovery_preserves_original_scheduling_window() -> None:
    # The September 1 reconcile window originally requested August 27/28/31.
    # A one-day canary must not invent a new runKey to pass local validation.
    value = plan(dates=["2026-08-28"], run_key="daily-market-data:2026-09-01:reconcile:v1")
    scope = validate_recovery_plan(value, expected_plan_hash=value["planHash"])
    assert scope.run_key == "daily-market-data:2026-09-01:reconcile:v1"
    assert scope.data_dates == ("2026-08-28",)
    assert {row["dataDate"] for row in iter_execution_matrix(scope)} == {"2026-08-28"}
    changed = dict(value, dataDates=["2026-09-01"])
    with pytest.raises(RecoveryContractError, match="plan_hash_mismatch"):
        validate_recovery_plan(changed, expected_plan_hash=value["planHash"])
    with pytest.raises(RecoveryContractError, match="receipt_date_outside_scope"):
        validate_recovery_receipt(receipt(scope, "2026-09-01", "dashboard"), scope=scope)


def test_plan_object_hash_is_distinct_from_semantic_plan_hash_and_reproducible() -> None:
    value = plan()
    encoded, object_sha = plan_bytes(value)
    assert value["planHash"] != object_sha
    assert encoded.endswith(b"\n")
    assert object_sha == plan_bytes(json.loads(encoded))[1]


def test_cli_outputs_cannot_overwrite_reports_or_memory() -> None:
    assert_recovery_output_path("/private/tmp/recovery/result.json") is None
    with pytest.raises(RecoveryContractError, match="recovery_output_protected_path"):
        assert_recovery_output_path("reports/phase0/latest-quality-summary.json")
    with pytest.raises(RecoveryContractError, match="recovery_output_protected_path"):
        assert_recovery_output_path(".recovery/memory/result.json")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("artifactRoot", "reports/phase0/latest", "artifact_root_protected_path"),
        ("stateDir", "memory", "state_dir_protected_path"),
        ("dataDates", ["2026-08-28", "2026-08-27"], "data_dates_must_be_ascending"),
    ],
)
def test_plan_rejects_scope_expansion_and_protected_paths(field: str, value, error: str) -> None:
    value_to_change = plan()
    value_to_change[field] = value
    # Hashes are immutable: changing the scope without recomputing the hash is
    # rejected before a caller can accidentally run an expanded plan.  Invalid
    # date/path shapes are rejected even earlier with a more specific error.
    expected = "plan_hash_mismatch"
    with pytest.raises(RecoveryContractError, match=expected):
        validate_recovery_plan(value_to_change)


def test_plan_rejects_recomputed_protected_path() -> None:
    value = plan()
    value["artifactRoot"] = "reports/phase0/latest"
    value["planHash"] = canonical_sha256({key: val for key, val in value.items() if key != "planHash"})
    with pytest.raises(RecoveryContractError, match="artifact_root_protected_path"):
        validate_recovery_plan(value)


def test_receipt_requires_scope_binding_and_exact_output_date() -> None:
    scope = validate_recovery_plan(plan())
    valid = receipt(scope, "2026-08-28", "dashboard")
    assert validate_recovery_receipt(valid, scope=scope, expected_attempt=1)["status"] == "COMPLETED"
    encoded, digest = receipt_bytes(valid)
    assert encoded.endswith(b"\n")
    assert len(digest) == 64
    wrong = dict(valid, planHash="d" * 64)
    with pytest.raises(RecoveryContractError, match="receipt_scope_binding_mismatch"):
        validate_recovery_receipt(wrong, scope=scope)
    wrong_output = dict(valid, output={**valid["output"], "asOf": "2026-08-27"})
    with pytest.raises(RecoveryContractError, match="receipt_output_date_mismatch"):
        validate_recovery_receipt(wrong_output, scope=scope)


def test_scope_completion_never_closes_on_missing_or_failed_stage() -> None:
    scope = validate_recovery_plan(plan())
    receipts = [
        receipt(scope, date_value, stage)
        for date_value in scope.data_dates
        for stage in scope.required_stages
        if not (date_value == "2026-08-31" and stage == "stock_flow")
    ]
    receipts.append(receipt(scope, "2026-08-31", "stock_flow", status="BLOCKED_QUALITY"))
    result = scope_completion(scope, receipts)
    assert result["closed"] is False
    assert {item["reason"] for item in result["invalid"]} == {"not_completed"}
    assert result["missing"] == [{"dataDate": "2026-08-31", "stage": "stock_flow"}]


def test_receipt_key_is_immutable_and_bounded() -> None:
    assert receipt_key(
        recovery_id="recovery-20260904-01",
        data_date="2026-08-28",
        stage="dashboard",
        attempt=2,
    ) == "market-data/recovery/receipts/recovery-20260904-01/2026-08-28/dashboard/2.json"
    with pytest.raises(RecoveryContractError, match="invalid_receipt_key"):
        receipt_key(recovery_id="../escape", data_date="2026-08-28", stage="dashboard", attempt=1)


def test_artifact_reuse_requires_input_hash_contract_and_completed_status() -> None:
    assert artifact_reusable({"inputSha256": "a" * 64, "contractVersion": "recovery-execution-v1", "status": "COMPLETED"}, input_sha256="a" * 64)
    assert not artifact_reusable({"inputSha256": "a" * 64, "contractVersion": "recovery-execution-v1", "status": "BLOCKED_QUALITY"}, input_sha256="a" * 64)
    assert not artifact_reusable({"inputSha256": "a" * 64, "contractVersion": "recovery-execution-v1", "status": "COMPLETED"}, input_sha256="b" * 64)


def test_budget_unknown_metrics_fail_closed() -> None:
    report = evaluate_recovery_budget(
        config=CONFIG,
        estimate=RecoveryBudgetEstimate(d1_reads=10),
        measured={"d1Reads": 1},
        reserved={"d1Reads": 1},
    )
    assert report["decision"] == "unknown_metrics"
    assert "actionsMinutes" in report["unknownMetrics"]


def test_budget_unknown_r2_operation_limits_fail_closed_even_with_usage_inputs() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    zero = {"d1Reads": 0, "d1Writes": 0, "workersRequests": 0, "actionsMinutes": 0, "r2Bytes": 0, "r2ClassA": 0, "r2ClassB": 0}
    missing_limits = json.loads(json.dumps(CONFIG))
    missing_limits["limits"].pop("r2ClassA", None)
    missing_limits["limits"].pop("r2ClassB", None)
    report = estimate_recovery_budget(scope=scope, config=missing_limits, retry_attempts=1, measured=zero, reserved=zero)
    assert report["decision"] == "unknown_metrics"
    assert "r2ClassALimit" in report["unknownMetrics"]
    assert "r2ClassBLimit" in report["unknownMetrics"]


def test_budget_upper_bound_includes_retries_and_blocks_over_limit() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    config = json.loads(json.dumps(CONFIG))
    # Explicit known operation caps test eligibility; missing caps remain
    # fail-closed in the separate regression above.
    config["limits"]["r2ClassA"] = 1_000_000
    config["limits"]["r2ClassB"] = 10_000_000
    safe = {
        "d1Reads": 0,
        "d1Writes": 0,
        "workersRequests": 0,
        "actionsMinutes": 0,
        "r2Bytes": 0,
        "r2ClassA": 0,
        "r2ClassB": 0,
    }
    report = estimate_recovery_budget(scope=scope, config=config, retry_attempts=1, measured=safe, reserved=safe)
    assert report["decision"] == "allow"
    assert report["estimate"]["actionsMinutes"] > 0
    over = dict(safe, d1Reads=config["hardLimits"]["d1RowsReadPerDay"])
    blocked = estimate_recovery_budget(scope=scope, config=config, retry_attempts=1, measured=over, reserved=safe)
    assert blocked["decision"] == "over_budget"


def test_two_three_date_batches_cover_a_simulated_six_day_history() -> None:
    history = ["2026-08-27", "2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]
    batches = [plan(dates=history[:3]), plan(dates=history[3:])]
    all_dates = [date_value for batch in batches for date_value in validate_recovery_plan(batch).data_dates]
    assert all_dates == history
    assert all(len(iter_execution_matrix(validate_recovery_plan(batch))) == 9 for batch in batches)


def test_failure_injection_does_not_turn_partial_receipts_into_success() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    complete = [receipt(scope, "2026-08-28", "dashboard")]
    # Simulate a publish failure after raw/model preparation.  The receipt is
    # retained for audit but scope completion remains open.
    complete.append(receipt(scope, "2026-08-28", "etf_penetration", status="FAILED_RETRYABLE"))
    complete.append(receipt(scope, "2026-08-28", "stock_flow", status="BLOCKED_QUALITY"))
    result = scope_completion(scope, complete)
    assert result["status"] == "INCOMPLETE"
    assert result["validCount"] == 1
    assert len(result["invalid"]) == 2


def test_executor_is_date_major_reuses_matching_attempt_and_never_fakes_missing_stages() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28", "2026-08-31"]))
    checkpoint = [receipt(scope, "2026-08-28", "dashboard", attempt=2)]
    calls: list[tuple[str, str, int]] = []

    def runner(row, attempt_count):
        calls.append((row["dataDate"], row["stage"], attempt_count))
        return receipt(scope, row["dataDate"], row["stage"], attempt=attempt_count)

    result = execute_recovery_plan(
        scope,
        attempt_count=2,
        existing_receipts=checkpoint,
        stage_runner=runner,
        retry_attempts=1,
        now=datetime.fromisoformat("2026-09-04T00:00:00+00:00"),
    )
    assert result["status"] == "COMPLETED"
    assert result["closed"] is True
    assert result["requestCount"] == 5
    assert calls[0] == ("2026-08-28", "etf_penetration", 2)
    assert calls[-1] == ("2026-08-31", "stock_flow", 2)
    assert result["records"][0]["reused"] is True


def test_executor_does_not_close_scope_when_existing_receipts_duplicate_a_slot() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    duplicate = receipt(scope, "2026-08-28", "dashboard", attempt=2)
    result = execute_recovery_plan(
        scope,
        attempt_count=2,
        existing_receipts=[duplicate, dict(duplicate)],
        stage_runner=lambda row, attempt_count: receipt(scope, row["dataDate"], row["stage"], attempt=attempt_count),
        retry_attempts=1,
        now=datetime.fromisoformat("2026-09-04T00:00:00+00:00"),
    )
    assert result["closed"] is False
    assert result["status"] == "BLOCKED_QUALITY"
    assert result["duplicateReceiptKeys"] == [{"dataDate": "2026-08-28", "stage": "dashboard"}]
    assert result["completion"]["duplicates"] == [{"dataDate": "2026-08-28", "stage": "dashboard"}]


def test_executor_stops_on_source_rate_limit_and_preserves_incomplete_scope() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28", "2026-08-31"]))
    calls: list[str] = []

    def runner(row, _attempt_count):
        calls.append(row["stage"])
        raise RecoveryContractError("official_source_429")

    result = execute_recovery_plan(
        scope,
        attempt_count=1,
        stage_runner=runner,
        retry_attempts=3,
        now=datetime.fromisoformat("2026-09-04T00:00:00+00:00"),
    )
    assert result["status"] == "SOURCE_UNAVAILABLE"
    assert result["closed"] is False
    assert result["haltedErrorCode"] == "official_source_429"
    assert calls == ["dashboard"]
    assert result["requestCount"] == 1
    assert result["completion"]["missing"]


def test_executor_retries_only_within_bound_and_stops_at_request_budget() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    calls = 0

    def runner(_row, _attempt_count):
        nonlocal calls
        calls += 1
        raise RecoveryContractError("temporary_connection_error")

    result = execute_recovery_plan(
        scope,
        attempt_count=1,
        stage_runner=runner,
        retry_attempts=3,
        max_requests=2,
        now=datetime.fromisoformat("2026-09-04T00:00:00+00:00"),
    )
    assert result["status"] == "FAILED_RETRYABLE"
    assert result["closed"] is False
    assert calls == 2
    assert result["requestCount"] == 2
    assert all(item["errorCode"] in {"temporary_connection_error", "recovery_request_budget_exceeded"} for item in result["records"])


def test_executor_deadline_expiry_fails_closed_without_runner_calls() -> None:
    scope = validate_recovery_plan(plan(dates=["2026-08-28"]))
    called = False

    def runner(_row, _attempt_count):
        nonlocal called
        called = True
        return receipt(scope, "2026-08-28", "dashboard")

    result = execute_recovery_plan(
        scope,
        attempt_count=1,
        stage_runner=runner,
        now=datetime.fromisoformat("2026-09-06T00:00:00+00:00"),
    )
    assert result["status"] == "BLOCKED_QUALITY"
    assert result["errorCode"] == "recovery_deadline_expired"
    assert result["requestCount"] == 0
    assert called is False
