from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from trading_desk.pipeline_checkpoint_bundle import CheckpointBundleError, build_stage_bundle
from trading_desk.pipeline_checkpoint_preflight import checkpoint_preflight
from trading_desk.pipeline_checkpoint_preflight import fresh_candidate_request_envelope, FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile
from trading_desk.pipeline_checkpoint_preflight import ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile
from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS, ResourceLimits
from test_pipeline_checkpoint_bundle import fixture, IDENTITY, INPUT
from test_pipeline_checkpoint_lifecycle import lifecycle_fixture

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("resume", [False, True])
def test_task_cost_aggregates_all_dimensions_and_both_attempts(resume):
    from trading_desk.pipeline_checkpoint_preflight import candidate_task_cost_upper
    profile = (ResumeCandidateRequestProfile if resume else FreshCandidateRequestProfile)("a" * 64, 7)
    unit = BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1))
    result = candidate_task_cost_upper(profile=profile,
        stage_costs=dict.fromkeys(("build", "verify", "publish"), unit),
        request_costs=dict.fromkeys(("lease", "budget", "candidate", "checkpoint"), unit),
        runtime_cost=unit)
    expected = 508 + 7 if resume else 236 + 4
    assert all(metric.value == expected for metric in result.metrics.values())
    assert all(metric.basis == "estimated" for metric in result.metrics.values())


def test_task_cost_does_not_infer_provider_rows_from_request_count():
    from trading_desk.pipeline_checkpoint_preflight import candidate_task_cost_upper
    unit = BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1))
    unknown_rows = BudgetVector.estimated({**dict.fromkeys(RESOURCE_DIMENSIONS, 1), "d1RowsRead": None})
    result = candidate_task_cost_upper(profile=FreshCandidateRequestProfile("a" * 64, 7),
        stage_costs=dict.fromkeys(("build", "verify", "publish"), unit),
        request_costs={"lease": unknown_rows, "budget": unit, "candidate": unit, "checkpoint": unit},
        runtime_cost=unit)
    assert result.metrics["d1RowsRead"].value is None
    assert not result.all_known()
    assert result.metrics["workerRequests"].value == 240


def test_explicit_fresh_profile_derives_finite_fault_and_renewal_envelope():
    profile = FreshCandidateRequestProfile("a" * 64, 7)
    assert profile.envelope()["requestsUpper"] == {"lease": 89, "budget": 118, "candidate": 22, "checkpoint": 7}
    assert profile.envelope()["workerRequestsUpper"] == 236
    assert profile.envelope()["liveExecutionAllowed"] is False
    assert FreshCandidateRequestProfile("a" * 64, 7, 15).envelope()["requestsUpper"]["lease"] == 70


def test_recovery_profile_counts_approval_and_lease_for_each_boundary():
    report = FreshRecoveryCandidateRequestProfile("a" * 64, 7,
        recovery_id="fixture-recovery", plan_sha256="b" * 64).envelope()
    assert report["normalRequests"] == {"lease": 130, "budget": 37, "candidate": 21, "checkpoint": 6}
    assert report["requestsUpper"] == {"lease": 198, "budget": 118, "candidate": 22, "checkpoint": 7}
    assert report["normalWorkerRequests"] == 194
    assert report["workerRequestsUpper"] == 345
    assert report["liveExecutionAllowed"] is False


def test_resume_envelope_retains_first_attempt_and_bounds_downloads_and_adoption():
    ordinary = ResumeCandidateRequestProfile("a" * 64, 7).envelope()
    recovery = ResumeRecoveryCandidateRequestProfile("a" * 64, 7,
        recovery_id="fixture-recovery", plan_sha256="b" * 64).envelope()
    assert ordinary["requestsUpper"] == {"lease": 103, "budget": 136, "candidate": 22, "checkpoint": 11}
    assert ordinary["workerRequestsUpper"] == 272
    assert ordinary["taskWorkerRequestsUpper"] == 236 + 272
    assert recovery["requestsUpper"] == {"lease": 226, "budget": 136, "candidate": 22, "checkpoint": 11}
    assert recovery["workerRequestsUpper"] == 395
    assert recovery["taskWorkerRequestsUpper"] == 345 + 395
    assert ordinary["liveExecutionAllowed"] is recovery["liveExecutionAllowed"] is False


@pytest.mark.parametrize("approved_requests,wrong_hash", [(344, False), (345, False), (600, True)])
def test_recovery_profile_requires_whole_approved_envelope(approved_requests, wrong_hash):
    from trading_desk.pipeline_application import canonical_sha256
    from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
    from trading_desk.pipeline_runner import PipelineRunnerError
    from test_pipeline_recovery_scope import fixture
    from test_pipeline_recovery_lease import Session
    from test_pipeline_lease import Clock
    plan, _ = fixture(("dashboard", "publish"))
    plan.pop("planHash")
    plan["maxRequests"] = approved_requests
    plan["planHash"] = canonical_sha256(plan)
    profile = FreshRecoveryCandidateRequestProfile("a" * 64, 7, recovery_id=plan["recoveryId"],
        plan_sha256="c" * 64 if wrong_hash else plan["planHash"])
    clock = Clock()
    session = Session(clock, plan)
    args = dict(approved_plan=plan, stages=["build", "verify", "publish"],
        base_url="https://flow.xiaoker.com", ingest_key="fixture-only", access_client_id="fixture-id",
        access_client_secret="fixture-secret", expected_attempt_count=1, request_profile=profile,
        session=session, now=clock.now, monotonic=clock.monotonic)
    if approved_requests < 345 or wrong_hash:
        with pytest.raises(PipelineRunnerError, match="request_profile_invalid"):
            RecoveryPipelineLease(**args)
    else:
        lease = RecoveryPipelineLease(**args)
        assert lease._maximum_requests == 198
    assert session.calls == []


def test_profile_covers_endpoint_artifact_ceiling_without_changing_legacy_limits():
    ordinary = FreshCandidateRequestProfile("a" * 64, 20).envelope()
    recovery = FreshRecoveryCandidateRequestProfile("a" * 64, 20,
        recovery_id="fixture", plan_sha256="b" * 64).envelope()
    assert ordinary["requestsUpper"] == {"lease": 167, "budget": 235, "candidate": 61, "checkpoint": 7}
    assert ordinary["workerRequestsUpper"] == 470
    assert recovery["workerRequestsUpper"] == 657
    assert ordinary["legacyLimits"] == {"lease": 32, "budget": 32, "candidate": 40, "checkpoint": 12}
    assert ordinary["liveExecutionAllowed"] is recovery["liveExecutionAllowed"] is False


@pytest.mark.parametrize("args", [("not-hash", 7), ("a" * 64, True), ("a" * 64, 21),
    ("a" * 64, 7, 301), ("a" * 64, 7, True)])
def test_fresh_profile_rejects_unsupported_or_unbounded_plan(args):
    with pytest.raises(ValueError):
        FreshCandidateRequestProfile(*args)


def test_full_candidate_envelope_includes_export_verification_and_settlement():
    report = fresh_candidate_request_envelope(stage_count=3, artifact_count=7,
        renewal_requests=0, revision_conflicts=0, checkpoint_readbacks=0,
        candidate_reconciliations=0)
    assert report["normalRequests"] == {"lease": 65, "budget": 37, "candidate": 21, "checkpoint": 6}
    assert report["normalExceededClients"] == ["budget", "lease"]
    assert report["workerRequestsUpper"] == 129
    assert report["liveExecutionAllowed"] is False
    assert report["providerRows"] == report["accountRemaining"] == "UNKNOWN"


def test_candidate_envelope_faults_are_additive_not_replacement_capacity():
    report = fresh_candidate_request_envelope(stage_count=3, artifact_count=7,
        renewal_requests=2, revision_conflicts=1, checkpoint_readbacks=1,
        candidate_reconciliations=1)
    assert report["requestsUpper"] == {"lease": 71, "budget": 41, "candidate": 22, "checkpoint": 7}
    assert report["workerRequestsUpper"] == 141


def test_unknown_candidate_fault_envelope_cannot_infer_zero():
    report = fresh_candidate_request_envelope(stage_count=6, artifact_count=7,
        renewal_requests=None, revision_conflicts=None, checkpoint_readbacks=None,
        candidate_reconciliations=None)
    assert report["normalRequests"] == {"lease": 86, "budget": 49, "candidate": 21, "checkpoint": 12}
    assert report["requestsUpper"] is None
    assert report["workerRequestsUpper"] is None
    assert len(report["unknownComponents"]) == 4


@pytest.mark.parametrize("field,value", [("stage_count", True), ("stage_count", 7),
    ("artifact_count", 0), ("artifact_count", 21), ("revision_conflicts", -1),
    ("renewal_requests", True)])
def test_candidate_envelope_rejects_unbounded_or_invalid_inputs(field, value):
    args = dict(stage_count=3, artifact_count=7, renewal_requests=0,
        revision_conflicts=0, checkpoint_readbacks=0, candidate_reconciliations=0)
    args[field] = value
    with pytest.raises(ValueError):
        fresh_candidate_request_envelope(**args)


def arguments(tmp_path):
    workspace, checkpoint = fixture(tmp_path)
    data, digest = build_stage_bundle(workspace=workspace, checkpoint=checkpoint,
                                     identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    config = json.loads((ROOT / "config/resource-budget.json").read_text())
    limits = replace(ResourceLimits.from_mapping(config), r2_class_a=1000, r2_class_b=1000)
    zero = {name: 0 for name in RESOURCE_DIMENSIONS}
    cost = dict(zero, r2Bytes=len(data), r2ClassA=1, r2ClassB=2, workerRequests=2, d1RowsRead=20)
    return dict(data=data, sha256=digest, identity=IDENTITY, stage="core_collection", input_sha256=INPUT,
                limits=limits, measured=BudgetVector.measured(zero, measured_at=NOW.isoformat()),
                reserved=BudgetVector.estimated(zero), task_cost=BudgetVector.estimated(cost),
                now=NOW, measurement_ttl_seconds=3600,
                lifecycle_evidence=dict(lifecycle_fixture(), observedAt=NOW.isoformat(),
                    referenceAudit=dict(lifecycle_fixture()["referenceAudit"], observedAt=NOW.isoformat()),
                    restoreEvidence=dict(lifecycle_fixture()["restoreEvidence"], observedAt=NOW.isoformat())))


def test_verified_exact_sizes_deterministic_no_execution_authority(tmp_path):
    args = arguments(tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    report = checkpoint_preflight(**args)
    assert report == checkpoint_preflight(**args)
    assert report["status"] == "LOCAL_INPUTS_CHECKED"
    assert report["artifact"]["encodedBytes"] == len(args["data"])
    assert report["artifact"]["fileCount"] == 2
    assert report["liveExecutionAllowed"] is False
    assert not any(report["sideEffects"].values())
    assert before == sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("change", ["unknown", "stale", "limit", "over"])
def test_incomplete_or_exhausted_budget_blocks(tmp_path, change):
    args = arguments(tmp_path)
    if change == "unknown":
        args["reserved"] = BudgetVector.unknown()
    elif change == "stale":
        args["measured"] = BudgetVector.measured({name: 0 for name in RESOURCE_DIMENSIONS}, measured_at="2026-10-01T00:00:00Z")
    elif change == "limit":
        args["limits"] = replace(args["limits"], r2_class_a=None)
    else:
        args["limits"] = replace(args["limits"], r2_bytes=1)
    report = checkpoint_preflight(**args)
    assert report["status"] == "BLOCKED"
    assert report["liveExecutionAllowed"] is False


def test_underestimated_transport_cannot_pass_as_zero_cost(tmp_path):
    args = arguments(tmp_path)
    args["task_cost"] = BudgetVector.estimated({name: 0 for name in RESOURCE_DIMENSIONS})
    report = checkpoint_preflight(**args)
    assert report["understatedDimensions"] == ["r2Bytes", "r2ClassA", "r2ClassB", "workerRequests"]
    assert "task_cost_below_transport_floor" in report["blockers"]


def test_missing_lifecycle_cannot_pass_offline_preflight(tmp_path):
    args = arguments(tmp_path)
    args.pop("lifecycle_evidence")
    report = checkpoint_preflight(**args)
    assert report["status"] == "BLOCKED"
    assert "checkpoint_lifecycle_inputs_incomplete" in report["blockers"]
    assert report["lifecycle"]["productionVerified"] is False


def test_size_limit_diagnostic_does_not_raise_transport_cap(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr("trading_desk.pipeline_checkpoint_preflight.MAX_OBJECT_BYTES", len(args["data"]) - 1)
    report = checkpoint_preflight(**args)
    assert report["artifact"]["fitsTransport"] is False
    assert "transport_object_size_exceeded" in report["blockers"]


def test_corrupt_bundle_cannot_generate_admissible_report(tmp_path):
    args = arguments(tmp_path)
    args["data"] += b"x"
    with pytest.raises(CheckpointBundleError):
        checkpoint_preflight(**args)


@pytest.mark.parametrize("ttl", [None, 0, -1, True])
def test_no_unbounded_measurement_freshness(tmp_path, ttl):
    args = arguments(tmp_path)
    args["measurement_ttl_seconds"] = ttl
    with pytest.raises(ValueError, match="ttl|required"):
        checkpoint_preflight(**args)
