from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from trading_desk.pipeline_adapters import PipelineAdapterError
from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_verify_adapter import CoreReadModelVerifyAdapter
from trading_desk.read_models import canonical_json_bytes
from test_pipeline_build_adapter import inputs


def setup(tmp_path, publication_status="confirmed"):
    build, args, snapshot = inputs(tmp_path)
    snapshot["publication"] = {"status": publication_status}
    build.snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    args["config"]["snapshotSha256"] = hashlib.sha256(build.snapshot_path.read_bytes()).hexdigest()
    args["input_sha256"] = canonical_sha256(args["config"])
    result = build.run(**args)
    config = {"buildInputSha256": args["input_sha256"], "buildOutputSha256": result["outputSha256"]}
    return CoreReadModelVerifyAdapter(args["workspace"]), dict(context=args["context"],
        config=config, input_sha256=canonical_sha256(config), workspace=tmp_path / "verify")


def checkpoint(args, result):
    return {"identity": args["context"].identity.as_dict(), "stage": "verify", "status": "COMPLETED",
        "inputSha256": args["input_sha256"], "outputSha256": result["outputSha256"]}


def test_existing_validator_and_saved_resume(tmp_path):
    adapter, args = setup(tmp_path)
    result = adapter.run(**args)
    assert result["evidence"]["publicationPerformed"] is False
    assert result["evidence"]["artifactCount"] == 7
    adapter.source_dir.joinpath("manifest.json").unlink()
    assert adapter.run(**args) == result
    assert adapter.verify_checkpoint(context=args["context"], checkpoint=checkpoint(args, result), workspace=args["workspace"])


@pytest.mark.parametrize("status", ["pending", "blocked", None])
def test_valid_build_without_confirmed_publication_is_not_publishable(tmp_path, status):
    adapter, args = setup(tmp_path, publication_status=status)
    with pytest.raises(PipelineAdapterError, match="verify_publication_not_confirmed"):
        adapter.run(**args)
    assert not args["workspace"].exists()


def test_lease_lost_after_copy_cannot_complete(tmp_path):
    adapter, args = setup(tmp_path)
    calls = []
    def lease():
        calls.append(True)
        if len(calls) == 3:
            raise RuntimeError("lease lost")
    args["context"] = replace(args["context"], lease_check=lease)
    with pytest.raises(RuntimeError, match="lease lost"):
        adapter.run(**args)
    assert not (args["workspace"] / "adapter-receipt.json").exists()


@pytest.mark.parametrize("fault", ["digest", "identity", "artifact", "missing", "symlink", "self_hash", "publication", "contract", "budget"])
def test_bad_candidate_rejected_before_output(tmp_path, fault, monkeypatch):
    adapter, args = setup(tmp_path)
    manifest_path = adapter.source_dir / "manifest.json"
    if fault == "digest":
        args["input_sha256"] = "0" * 64
    elif fault == "identity":
        args["context"] = replace(args["context"], identity=replace(args["context"].identity, target_date="2026-08-05"))
    elif fault == "artifact":
        (adapter.source_dir / "radar.json").write_bytes(b"{}")
    elif fault == "missing":
        (adapter.source_dir / "overview.json").unlink()
    elif fault == "symlink":
        target = adapter.source_dir / "overview.json"
        target.rename(tmp_path / "outside.json")
        target.symlink_to(tmp_path / "outside.json")
    elif fault == "budget":
        monkeypatch.setattr("trading_desk.pipeline_verify_adapter.MAX_SAVED_BYTES", 1)
    else:
        manifest = json.loads(manifest_path.read_bytes())
        if fault == "self_hash":
            manifest["manifestSha256"] = "0" * 64
        else:
            if fault == "publication":
                manifest["publication"]["status"] = "pending"
            else:
                manifest["capabilityContract"] = {}
            unsigned = {key: value for key, value in manifest.items() if key != "manifestSha256"}
            manifest["manifestSha256"] = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(PipelineAdapterError):
        adapter.run(**args)
    assert not args["workspace"].exists()


@pytest.mark.parametrize("filename", ["build/manifest.json", "build/radar.json", "adapter-receipt.json", "extra.json"])
def test_saved_corruption_rejected(tmp_path, filename):
    adapter, args = setup(tmp_path)
    result = adapter.run(**args)
    (args["workspace"] / filename).write_bytes(b"{}")
    assert not adapter.verify_checkpoint(context=args["context"], checkpoint=checkpoint(args, result), workspace=args["workspace"])


def test_lease_loss_and_partial_output(tmp_path):
    adapter, args = setup(tmp_path)
    def lost():
        raise RuntimeError("lease lost")
    args["context"] = replace(args["context"], lease_check=lost)
    with pytest.raises(RuntimeError, match="lease lost"):
        adapter.run(**args)
    assert not args["workspace"].exists()
    args["context"] = replace(args["context"], lease_check=None)
    args["workspace"].mkdir()
    (args["workspace"] / "partial").write_bytes(b"x")
    with pytest.raises(PipelineAdapterError, match="partial_verify"):
        adapter.run(**args)


def test_cross_job_runner_resume(tmp_path):
    from trading_desk.pipeline_runner import run_pipeline_request
    from test_pipeline_bundle_resume import ROOT, LOCK, Guard, payload
    adapter, args = setup(tmp_path)
    request = payload()
    request["identity"]["targetDate"] = "2026-08-06"
    request["stages"] = ["verify"]
    request["stageConfigs"] = {"verify": args["config"]}
    request["stageInputs"] = {"verify": args["input_sha256"]}
    common = dict(producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
        now=datetime(2026, 10, 3, tzinfo=timezone.utc), stage_adapters={"verify": adapter},
        lease_guard=Guard(), export_bundles=True)
    first = run_pipeline_request(request, state_dir=tmp_path / "first/state", output_path=tmp_path / "first/report.json", **common)
    ref = first["checkpointBundles"]["exports"]["verify"]
    data = Path(ref["path"]).read_bytes()
    adapter.source_dir.joinpath("manifest.json").unlink()
    request["lease"].update(owner="second-owner", callbackOwner="second-owner", attemptCount=2)
    second = run_pipeline_request(request, state_dir=tmp_path / "second/state", output_path=tmp_path / "second/report.json",
        resume_bundles={"verify": (data, ref["sha256"])}, **common)
    assert second["status"] == "COMPLETED"
    assert second["plan"]["records"][0]["reused"] is True
