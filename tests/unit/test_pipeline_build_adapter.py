from dataclasses import replace
import hashlib
import json

import pytest

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_adapters import PipelineAdapterError
from trading_desk.pipeline_build_adapter import CoreReadModelBuildAdapter
from trading_desk.pipeline_runner import build_execution_context
from trading_desk.read_models import build_market_read_models, canonical_json_bytes
from trading_desk.read_model_capabilities import CAPABILITY_CONTRACT_VERSION
from test_pipeline_bundle_resume import payload
from test_read_models import _snapshot
from datetime import datetime, timezone


def inputs(tmp_path):
    snapshot = _snapshot()
    data = canonical_json_bytes(snapshot)
    path = tmp_path / "source.json"; path.write_bytes(data)
    request = payload()
    request["identity"]["targetDate"] = snapshot["asOf"]
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=datetime(2026, 10, 3, tzinfo=timezone.utc))
    config = {"snapshotSha256": hashlib.sha256(data).hexdigest()}
    return CoreReadModelBuildAdapter(path), dict(context=context, config=config,
        input_sha256=canonical_sha256(config), workspace=tmp_path / "build"), snapshot


def test_builder_equivalence_and_offline_resume(tmp_path):
    adapter, args, snapshot = inputs(tmp_path)
    result = adapter.run(**args)
    payloads, manifest = build_market_read_models(snapshot, require_materialized=True,
        producer_build_sha="a" * 40, capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    for name, value in payloads.items():
        assert (args["workspace"] / f"{name}.json").read_bytes() == canonical_json_bytes(value)
    assert (args["workspace"] / "manifest.json").read_bytes() == canonical_json_bytes(manifest)
    adapter.snapshot_path.unlink()  # Cross-job resume uses its saved exact input.
    assert adapter.run(**args) == result
    assert result["evidence"]["publicationPerformed"] is False


@pytest.mark.parametrize("change", ["hash", "date", "config"])
def test_invalid_inputs_fail_before_output(tmp_path, change):
    adapter, args, _ = inputs(tmp_path)
    if change == "hash":
        adapter.snapshot_path.write_bytes(b"{}")
    elif change == "date":
        args["context"] = replace(args["context"], identity=replace(args["context"].identity, target_date="2026-08-05"))
    else:
        args["input_sha256"] = "0" * 64
    with pytest.raises(PipelineAdapterError):
        adapter.run(**args)
    assert not args["workspace"].exists()


@pytest.mark.parametrize("file", ["manifest.json", "input-snapshot.json", "bootstrap.json", "adapter-receipt.json"])
def test_corruption_rejects_checkpoint(tmp_path, file):
    adapter, args, _ = inputs(tmp_path)
    result = adapter.run(**args)
    checkpoint = {"inputSha256": args["input_sha256"], "outputSha256": result["outputSha256"],
                  "identity": args["context"].identity.as_dict(), "stage": "build", "status": "COMPLETED"}
    assert adapter.verify_checkpoint(context=args["context"], checkpoint=checkpoint, workspace=args["workspace"])
    target = args["workspace"] / file
    if file == "adapter-receipt.json":
        value = json.loads(target.read_bytes()); value["stageResult"]["evidence"]["publicationPerformed"] = True
        target.write_text(json.dumps(value))
    else:
        target.write_bytes(b"{}")
    assert not adapter.verify_checkpoint(context=args["context"], checkpoint=checkpoint, workspace=args["workspace"])
    with pytest.raises(PipelineAdapterError):
        adapter.run(**args)


def test_partial_workspace_requires_reconciliation(tmp_path):
    adapter, args, _ = inputs(tmp_path)
    args["workspace"].mkdir(); (args["workspace"] / "partial.json").write_text("{}")
    with pytest.raises(PipelineAdapterError, match="partial_build"):
        adapter.run(**args)


def test_input_size_bound_and_workspace_symlink(tmp_path, monkeypatch):
    adapter, args, _ = inputs(tmp_path)
    monkeypatch.setattr("trading_desk.pipeline_build_adapter.MAX_INPUT_BYTES", 1)
    with pytest.raises(PipelineAdapterError, match="size"):
        adapter.run(**args)
    outside = tmp_path / "outside"; outside.mkdir()
    args["workspace"].symlink_to(outside, target_is_directory=True)
    with pytest.raises(PipelineAdapterError, match="symlink"):
        adapter.run(**args)
    assert list(outside.iterdir()) == []


def test_real_runner_build_stage_resume_without_original_input(tmp_path):
    from trading_desk.pipeline_runner import run_pipeline_request
    from test_pipeline_bundle_resume import ROOT, LOCK, Guard
    adapter, args, _ = inputs(tmp_path)
    request = payload()
    request["identity"]["targetDate"] = "2026-08-06"
    request["stages"] = ["build"]
    request["stageConfigs"] = {"build": args["config"]}
    request["stageInputs"] = {"build": args["input_sha256"]}
    options = dict(state_dir=tmp_path / "state", output_path=tmp_path / "report.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
        now=datetime(2026, 10, 3, tzinfo=timezone.utc), stage_adapters={"build": adapter},
        lease_guard=Guard())
    first = run_pipeline_request(request, **options)
    assert first["status"] == "COMPLETED"
    adapter.snapshot_path.unlink()
    second = run_pipeline_request(request, **options)
    assert second["status"] == "COMPLETED"
    assert second["plan"]["records"][0]["reused"] is True


def test_cross_job_bundle_resume_uses_saved_input(tmp_path):
    from pathlib import Path
    from trading_desk.pipeline_runner import run_pipeline_request
    from test_pipeline_bundle_resume import ROOT, LOCK, Guard
    adapter, args, _ = inputs(tmp_path)
    request = payload()
    request["identity"]["targetDate"] = "2026-08-06"
    request["stages"] = ["build"]
    request["stageConfigs"] = {"build": args["config"]}
    request["stageInputs"] = {"build": args["input_sha256"]}
    common = dict(producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
                  now=datetime(2026, 10, 3, tzinfo=timezone.utc), stage_adapters={"build": adapter},
                  lease_guard=Guard(), export_bundles=True)
    first = run_pipeline_request(request, state_dir=tmp_path / "first/state",
                                 output_path=tmp_path / "first/report.json", **common)
    ref = first["checkpointBundles"]["exports"]["build"]
    data = Path(ref["path"]).read_bytes()
    adapter.snapshot_path.unlink()
    request["lease"].update(owner="second-owner", callbackOwner="second-owner", attemptCount=2)
    second = run_pipeline_request(request, state_dir=tmp_path / "second/state",
                                  output_path=tmp_path / "second/report.json",
                                  resume_bundles={"build": (data, ref["sha256"])}, **common)
    assert second["status"] == "COMPLETED"
    assert second["plan"]["records"][0]["reused"] is True
    assert second["sideEffects"]["remoteWrites"] is False


@pytest.mark.parametrize("fail_at", [1, 2])
def test_lease_loss_before_write_and_output_bound(tmp_path, monkeypatch, fail_at):
    adapter, args, _ = inputs(tmp_path)
    calls = 0
    def lost():
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise RuntimeError("lease_lost")
    args["context"] = replace(args["context"], lease_check=lost)
    with pytest.raises(RuntimeError, match="lease_lost"):
        adapter.run(**args)
    assert not args["workspace"].exists()
    assert calls == fail_at
    args["context"] = replace(args["context"], lease_check=None)
    monkeypatch.setattr("trading_desk.pipeline_build_adapter.MAX_OUTPUT_BYTES", 1)
    with pytest.raises(PipelineAdapterError, match="output_budget"):
        adapter.run(**args)
    assert not args["workspace"].exists()
