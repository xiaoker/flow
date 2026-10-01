from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk.pipeline_application import (
    ApplicationContractError,
    build_application_identity,
    build_application_plan,
    build_checkpoint,
    canonical_sha256,
    checkpoint_is_reusable,
    execute_application_plan,
    retry_bounded,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "pipeline" / "v060-a-redacted.json"


def _identity():
    return build_application_identity(
        run_key="daily-market-data:2026-09-30:reconcile:v1",
        window_date="2026-09-30",
        target_date="2026-09-29",
        producer_build_sha="a" * 64,
    )


def test_same_redacted_fixture_has_stable_plan_and_checkpoint_identity() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    identity = build_application_identity(**{
        "run_key": fixture["identity"]["runKey"],
        "window_date": fixture["identity"]["windowDate"],
        "target_date": fixture["identity"]["targetDate"],
        "producer_build_sha": fixture["identity"]["producerBuildSha"],
    })
    plan_a = build_application_plan(identity, ["build"], stage_inputs={"build": fixture["inputSha256"]})
    plan_b = build_application_plan(identity, ["build"], stage_inputs={"build": fixture["inputSha256"]})
    assert plan_a == plan_b
    checkpoint = build_checkpoint(
        identity=identity,
        stage=fixture["stage"],
        status="COMPLETED",
        input_sha256=fixture["inputSha256"],
        output_sha256=fixture["outputSha256"],
        allowed_side_effects=fixture["allowedSideEffects"],
        evidence=fixture["evidence"],
    )
    # The legacy publisher/recovery receipt and the extracted checkpoint must
    # carry the same redacted identity, input/output artifacts and declared
    # side-effect set; only the new self-hash is additive.
    legacy_projection = {
        "identity": fixture["identity"],
        "stage": fixture["stage"],
        "inputSha256": fixture["inputSha256"],
        "outputSha256": fixture["outputSha256"],
        "allowedSideEffects": sorted(fixture["allowedSideEffects"]),
    }
    new_projection = {
        key: checkpoint[key]
        for key in ("identity", "stage", "inputSha256", "outputSha256", "allowedSideEffects")
    }
    assert new_projection == legacy_projection
    assert checkpoint["checkpointSha256"] == canonical_sha256(
        {key: value for key, value in checkpoint.items() if key != "checkpointSha256"}
    )
    assert checkpoint_is_reusable(
        checkpoint,
        identity=identity,
        stage="build",
        input_sha256=fixture["inputSha256"],
    )


def test_checkpoint_identity_or_input_change_invalidates_resume() -> None:
    checkpoint = build_checkpoint(
        identity=_identity(),
        stage="build",
        status="COMPLETED",
        input_sha256="b" * 64,
        output_sha256="c" * 64,
        allowed_side_effects=("filesystem:artifact",),
    )
    assert not checkpoint_is_reusable(checkpoint, identity=_identity(), stage="build", input_sha256="d" * 64)
    other = build_application_identity(
        run_key="daily-market-data:2026-09-30:reconcile:v1",
        window_date="2026-09-30",
        target_date="2026-09-29",
        producer_build_sha="d" * 64,
    )
    assert not checkpoint_is_reusable(checkpoint, identity=other, stage="build", input_sha256="b" * 64)


def test_executor_reuses_exact_checkpoint_and_runs_only_missing_stage() -> None:
    identity = _identity()
    input_hashes = {"build": "b" * 64, "verify": "d" * 64}
    existing = build_checkpoint(
        identity=identity,
        stage="build",
        status="COMPLETED",
        input_sha256=input_hashes["build"],
        output_sha256="c" * 64,
        allowed_side_effects=("filesystem:artifact",),
    )
    calls: list[str] = []

    def runner(stage: str, input_sha: str):
        calls.append(stage)
        return build_checkpoint(
            identity=identity,
            stage=stage,
            status="COMPLETED",
            input_sha256=input_sha,
            output_sha256="e" * 64,
            allowed_side_effects=("r2:immutable-put",),
        )

    result = execute_application_plan(
        identity=identity,
        stages=["build", "verify"],
        stage_inputs=input_hashes,
        existing_checkpoints=[existing],
        stage_runner=runner,
    )
    assert result["status"] == "COMPLETED"
    assert calls == ["verify"]
    assert result["records"] == [
        {"stage": "build", "status": "COMPLETED", "reused": True, "checkpointSha256": existing["checkpointSha256"]},
        {"stage": "verify", "status": "COMPLETED", "reused": False, "checkpointSha256": result["checkpoints"][1]["checkpointSha256"]},
    ]


def test_identity_rejects_window_and_run_key_date_mismatch() -> None:
    with pytest.raises(ApplicationContractError, match="run_key_window_date_mismatch"):
        build_application_identity(
            run_key="daily-market-data:2026-09-30:reconcile:v1",
            window_date="2026-10-01",
        )


def test_retry_policy_is_bounded_and_keeps_side_effects_injected() -> None:
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "artifact-id"

    assert retry_bounded(operation, max_attempts=3, sleep=lambda _seconds: None) == "artifact-id"
    assert len(calls) == 3
