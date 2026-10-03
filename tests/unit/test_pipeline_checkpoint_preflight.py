from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from trading_desk.pipeline_checkpoint_bundle import CheckpointBundleError, build_stage_bundle
from trading_desk.pipeline_checkpoint_preflight import checkpoint_preflight
from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS, ResourceLimits
from test_pipeline_checkpoint_bundle import fixture, IDENTITY, INPUT
from test_pipeline_checkpoint_lifecycle import lifecycle_fixture

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


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
