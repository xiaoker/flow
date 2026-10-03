from dataclasses import replace

import pytest

from test_pipeline_checkpoint_preflight import arguments
from trading_desk.pipeline_application import build_checkpoint
from trading_desk.pipeline_checkpoint_bundle import build_stage_bundle, CheckpointBundleError
from trading_desk.pipeline_checkpoint_preflight import pipeline_task_preflight
from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS


def task_arguments(tmp_path):
    base = arguments(tmp_path)
    identity = base["identity"]
    input_hash = "d" * 64
    checkpoint = build_checkpoint(identity=identity, stage="build", status="COMPLETED",
                                  input_sha256=input_hash, output_sha256="e" * 64)
    second = build_stage_bundle(workspace=tmp_path / "stage", checkpoint=checkpoint,
                               identity=identity, stage="build", input_sha256=input_hash)
    zero = {name: 0 for name in RESOURCE_DIMENSIONS}
    vector = lambda **values: BudgetVector.estimated(dict(zero, **values))
    return dict(identity=identity, stages=["core_collection", "build"],
                stage_inputs={"core_collection": base["input_sha256"], "build": input_hash},
                bundles={"core_collection": (base["data"], base["sha256"]), "build": second},
                stage_costs={"core_collection": vector(d1RowsRead=30), "build": vector(d1RowsRead=40)},
                overhead_costs={"lease_control": vector(d1RowsRead=20, workerRequests=3),
                    "platform_runtime": vector(actionsMinutes=10),
                    "checkpoint_io": vector(r2Bytes=len(base["data"]) + len(second[0]),
                                            r2ClassA=2, r2ClassB=4, workerRequests=4)},
                **{key: base[key] for key in ("limits", "measured", "reserved", "now",
                    "measurement_ttl_seconds", "lifecycle_evidence")})


def test_complete_task_is_stable_pure_and_not_execution_authority(tmp_path):
    args = task_arguments(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = pipeline_task_preflight(**args)
    assert report == pipeline_task_preflight(**args)
    assert report["status"] == "LOCAL_INPUTS_CHECKED"
    assert report["totals"]["encodedBytes"] == sum(len(v[0]) for v in args["bundles"].values())
    assert report["transportCostFloors"]["r2ClassB"] == 4
    assert report["liveExecutionAllowed"] is False
    assert report["resourceReservationPerformed"] is False
    assert not any(report["sideEffects"].values())
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    args["stage_inputs"] = dict(reversed(list(args["stage_inputs"].items())))
    assert pipeline_task_preflight(**args)["plan"]["planHash"] == report["plan"]["planHash"]


@pytest.mark.parametrize("component", ["stage", "lease_control", "checkpoint_io", "platform_runtime", "bundle", "account", "reservation", "lifecycle"])
def test_missing_evidence_fails_closed(tmp_path, component):
    args = task_arguments(tmp_path)
    if component == "stage":
        del args["stage_costs"]["build"]
    elif component in args["overhead_costs"]:
        del args["overhead_costs"][component]
    elif component == "bundle":
        del args["bundles"]["build"]
    elif component == "account":
        args["measured"] = BudgetVector.unknown()
    elif component == "reservation":
        args["reserved"] = BudgetVector.unknown()
    else:
        args["lifecycle_evidence"] = None
    assert pipeline_task_preflight(**args)["status"] == "BLOCKED"


def test_aggregate_cost_exceeds_cap_even_when_each_stage_fits(tmp_path):
    args = task_arguments(tmp_path)
    args["limits"] = replace(args["limits"], hard_rows_read_per_day=60)
    report = pipeline_task_preflight(**args)
    assert report["status"] == "BLOCKED"
    assert "resource_over_budget" in report["blockers"]


def test_other_stages_cannot_mask_checkpoint_underestimate(tmp_path):
    args = task_arguments(tmp_path)
    args["overhead_costs"]["checkpoint_io"] = BudgetVector.estimated({n: 0 for n in RESOURCE_DIMENSIONS})
    args["stage_costs"]["build"] = BudgetVector.estimated({n: 100 for n in RESOURCE_DIMENSIONS})
    report = pipeline_task_preflight(**args)
    assert "checkpoint_cost_below_aggregate_transport_floor" in report["blockers"]
    assert report["understatedDimensions"] == ["r2Bytes", "r2ClassA", "r2ClassB", "workerRequests"]


def test_corrupt_bundle_is_not_budget_evidence(tmp_path):
    args = task_arguments(tmp_path)
    args["bundles"]["build"] = (b"invalid", "a" * 64)
    with pytest.raises(CheckpointBundleError):
        pipeline_task_preflight(**args)


@pytest.mark.parametrize("change", ["stale", "unknown_stage", "missing_limit", "transport_size"])
def test_budget_and_transport_constraints_remain_effective(tmp_path, monkeypatch, change):
    args = task_arguments(tmp_path)
    if change == "stale":
        args["measured"] = BudgetVector.measured({n: 0 for n in RESOURCE_DIMENSIONS},
                                                measured_at="2026-10-01T00:00:00Z")
    elif change == "unknown_stage":
        args["stage_costs"]["build"] = BudgetVector.unknown()
    elif change == "missing_limit":
        args["limits"] = replace(args["limits"], r2_class_a=None)
    else:
        monkeypatch.setattr("trading_desk.pipeline_checkpoint_preflight.MAX_OBJECT_BYTES", 1)
    report = pipeline_task_preflight(**args)
    assert report["status"] == "BLOCKED"
    assert report["liveExecutionAllowed"] is False


@pytest.mark.parametrize("change", ["extra_stage_cost", "extra_input", "extra_overhead", "duplicate_stage", "bad_tuple", "bad_ttl"])
def test_invalid_contract_rejected(tmp_path, change):
    args = task_arguments(tmp_path)
    if change == "extra_stage_cost":
        args["stage_costs"]["verify"] = args["stage_costs"]["build"]
    elif change == "extra_input":
        args["stage_inputs"]["verify"] = "a" * 64
    elif change == "extra_overhead":
        args["overhead_costs"]["unbounded"] = args["stage_costs"]["build"]
    elif change == "duplicate_stage":
        args["stages"].append("build")
    elif change == "bad_tuple":
        args["bundles"]["build"] = [b"x", "a" * 64]
    else:
        args["measurement_ttl_seconds"] = True
    with pytest.raises(ValueError):
        pipeline_task_preflight(**args)
