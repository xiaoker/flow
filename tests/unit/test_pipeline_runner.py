from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_desk.pipeline_runner import (
    PipelineRunnerError,
    dependency_lock_digest,
    run_pipeline_request,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/pipeline/v060-c-ordinary.json"
LOCK = ROOT / "web/package-lock.json"
PRODUCER_SHA = "a" * 64


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _run(payload: dict, tmp_path: Path, *, producer_sha: str = PRODUCER_SHA) -> dict:
    return run_pipeline_request(
        payload,
        state_dir=tmp_path / "state",
        output_path=tmp_path / "result.json",
        producer_build_sha=producer_sha,
        dependency_lock_paths=[LOCK],
        project_root=ROOT,
        now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )


def test_local_and_ci_entry_contract_produces_stable_candidate_without_side_effects(tmp_path: Path) -> None:
    first = _run(_payload(), tmp_path)
    second = _run(_payload(), tmp_path)
    assert first["status"] == "COMPLETED"
    assert first["plan"]["planHash"] == second["plan"]["planHash"]
    assert all(record["reused"] for record in second["plan"]["records"])
    assert first["sideEffects"] == {
        "remoteWrites": False,
        "workflowDispatch": False,
        "activeReleaseChanged": False,
        "productionEntryEnabled": False,
        "officialSourceRequests": False,
        "officialSourceRequestCount": 0,
        "officialSourceRequestsAllowed": False,
        "isolatedFilesystemWrites": True,
    }
    assert first["executionMode"] == "fixture"
    assert first["dependencyLock"]["sha256"] == dependency_lock_digest([LOCK], root=ROOT)["sha256"]


def test_failed_checkpoint_can_resume_without_rerunning_completed_stages(tmp_path: Path) -> None:
    first_payload = _payload()
    first_payload["stageResults"]["verify"] = {
        "status": "FAILED_RETRYABLE",
        "allowedSideEffects": ["filesystem:checkpoint"],
        "evidence": {"reason": "fixture_interruption"},
    }
    first = _run(first_payload, tmp_path)
    assert first["status"] == "FAILED_RETRYABLE"

    resumed_payload = _payload()
    resumed = _run(resumed_payload, tmp_path)
    assert resumed["status"] == "COMPLETED"
    records = {item["stage"]: item for item in resumed["plan"]["records"]}
    assert records["core_collection"]["reused"] is True
    assert records["normalize"]["reused"] is True
    assert records["verify"]["reused"] is False


def test_recovery_requires_approved_plan_and_active_lease(tmp_path: Path) -> None:
    payload = _payload()
    plan_sha = "d" * 64
    payload["mode"] = "recovery"
    payload["authorization"] = {
        "kind": "approved-recovery",
        "verified": True,
        "credentialRef": "environment:existing-admin",
        "planSha256": plan_sha,
    }
    payload["recovery"] = {"recoveryId": "recovery-fixture-1", "planSha256": plan_sha}
    report = _run(payload, tmp_path)
    assert report["context"]["mode"] == "recovery"
    assert report["context"]["recovery"]["recoveryId"] == "recovery-fixture-1"

    late = copy.deepcopy(payload)
    late["lease"]["callbackOwner"] = "other-owner"
    with pytest.raises(PipelineRunnerError, match="late_lease_owner"):
        _run(late, tmp_path / "late")

    expired = copy.deepcopy(payload)
    expired["lease"]["expiresAt"] = "2026-09-30T23:59:59Z"
    with pytest.raises(PipelineRunnerError, match="lease_expired"):
        _run(expired, tmp_path / "expired")


def test_r2_success_d1_failure_is_not_reported_as_completed(tmp_path: Path) -> None:
    payload = _payload()
    payload["stageResults"]["verify"] = {
        "status": "FAILED_RETRYABLE",
        "allowedSideEffects": ["r2:immutable-put"],
        "evidence": {"r2Status": "SUCCEEDED", "d1Status": "FAILED"},
    }
    report = _run(payload, tmp_path)
    assert report["status"] == "FAILED_RETRYABLE"
    assert report["sideEffects"]["activeReleaseChanged"] is False

    unsafe = _payload()
    unsafe["stageResults"]["verify"]["evidence"] = {"r2Status": "SUCCEEDED", "d1Status": "FAILED"}
    with pytest.raises(PipelineRunnerError, match="partial_remote_side_effect_cannot_complete"):
        _run(unsafe, tmp_path / "unsafe")


def test_historical_target_preserves_latest_pointer_contract(tmp_path: Path) -> None:
    payload = _payload()
    payload["identity"]["targetDate"] = "2026-09-29"
    payload["latestDataDate"] = "2026-09-30"
    report = _run(payload, tmp_path)
    assert report["latestDataDate"] == "2026-09-30"
    assert report["sideEffects"]["activeReleaseChanged"] is False
    assert report["historyPolicy"] == "historical_target_does_not_rewrite_latest"


def test_lock_mismatch_and_protected_outputs_fail_closed(tmp_path: Path) -> None:
    payload = _payload()
    payload["dependencyLockSha256"] = "0" * 64
    with pytest.raises(PipelineRunnerError, match="dependency_lock_sha_mismatch"):
        _run(payload, tmp_path / "mismatch")

    with pytest.raises(PipelineRunnerError, match="protected_output_path"):
        run_pipeline_request(
            _payload(),
            state_dir=tmp_path / "memory" / "state",
            output_path=tmp_path / "result.json",
            producer_build_sha=PRODUCER_SHA,
            dependency_lock_paths=[LOCK],
            project_root=ROOT,
        )

    with pytest.raises(PipelineRunnerError, match="protected_output_path"):
        run_pipeline_request(
            _payload(),
            state_dir=ROOT / "reports" / "phase0" / "latest-manifest.json" / "nested",
            output_path=tmp_path / "safe-result.json",
            producer_build_sha=PRODUCER_SHA,
            dependency_lock_paths=[LOCK],
            project_root=ROOT,
        )


def test_cli_entry_is_the_same_bounded_fixture_runner(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_desk.cli",
            "pipeline",
            "--request",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path / "state"),
            "--output",
            str(tmp_path / "result.json"),
            "--dependency-lock",
            "web/package-lock.json",
            "--producer-build-sha",
            PRODUCER_SHA,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src:."},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert output["status"] == "COMPLETED"


def test_runner_accepts_exact_github_sha_and_persists_before_later_stage_error(tmp_path: Path) -> None:
    payload = _payload()
    producer_sha = "f" * 40
    payload["identity"]["producerBuildSha"] = producer_sha
    payload["stageResults"].pop("publish")

    with pytest.raises(PipelineRunnerError, match="stage_result_missing"):
        _run(payload, tmp_path, producer_sha=producer_sha)

    saved = json.loads((tmp_path / "state" / "checkpoints.json").read_text(encoding="utf-8"))
    assert saved["identity"]["producerBuildSha"] == producer_sha
    assert [checkpoint["stage"] for checkpoint in saved["checkpoints"]] == [
        "core_collection",
        "normalize",
        "build",
        "verify",
    ]


def test_candidate_workflow_installs_runtime_dependencies_before_cli() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text(encoding="utf-8")
    install = workflow.index("python -m pip install --disable-pip-version-check -e .")
    run_cli = workflow.index("trading_desk.cli pipeline")
    assert install < run_cli
