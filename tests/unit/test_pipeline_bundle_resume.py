import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_runner import PipelineRunnerError, dependency_lock_digest, run_pipeline_request

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "web/package-lock.json"


class Guard:
    renewal_count = 0

    def __init__(self, fail_at=None):
        self.calls = 0
        self.fail_at = fail_at

    def verify(self, context):
        return True

    def check(self):
        self.confirm()

    def confirm(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("lease_lost")

    def evidence(self):
        return {"fixture": True}


class Adapter:
    allowed_side_effects = ("filesystem:isolated_stage_workspace",)

    def __init__(self, reject=False):
        self.runs = 0
        self.reject = reject

    def run(self, *, workspace, **kwargs):
        self.runs += 1
        workspace.mkdir(parents=True)
        (workspace / "output.json").write_text('{"actual":123}')
        return {"status": "COMPLETED", "outputSha256": canonical_sha256({"actual": 123}),
                "allowedSideEffects": self.allowed_side_effects, "evidence": {"sourceDates": ["2026-09-29"]}}

    def verify_checkpoint(self, *, workspace, checkpoint, **kwargs):
        return not self.reject and json.loads((workspace / "output.json").read_text()) == {"actual": 123} and checkpoint["outputSha256"] == canonical_sha256({"actual": 123})


def payload():
    config = {"parserVersion": "1", "classificationVersion": "v3"}
    return {"identity": {"runKey": "daily-market-data:2026-09-30:reconcile:v1", "windowDate": "2026-09-30", "targetDate": "2026-09-29"},
            "mode": "ordinary", "lease": {"owner": "fixture-owner", "callbackOwner": "fixture-owner", "status": "ACTIVE", "expiresAt": "2099-01-01T00:00:00Z", "attemptCount": 1},
            "authorization": {"kind": "existing-admin", "verified": True},
            "dependencyLockSha256": dependency_lock_digest([LOCK], root=ROOT)["sha256"],
            "stages": ["core_collection"], "stageInputs": {"core_collection": canonical_sha256(config)},
            "stageConfigs": {"core_collection": config}}


def run(root, *, adapter=None, guard=None, bundles=None, export=True, request=None):
    return run_pipeline_request(request or payload(), state_dir=root / "state", output_path=root / "result.json",
                                producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
                                now=datetime(2026, 10, 1, tzinfo=timezone.utc), stage_adapters={"core_collection": adapter or Adapter()},
                                lease_guard=guard or Guard(), resume_bundles=bundles, export_bundles=export)


def exported(tmp_path):
    report = run(tmp_path / "first")
    ref = report["checkpointBundles"]["exports"]["core_collection"]
    return {"core_collection": (Path(ref["path"]).read_bytes(), ref["sha256"])}


def test_two_separate_job_directories_resume_without_source_stage(tmp_path):
    bundles = exported(tmp_path)
    adapter = Adapter()
    request = payload()
    request["lease"].update(owner="new-owner", callbackOwner="new-owner", attemptCount=2)
    result = run(tmp_path / "second", adapter=adapter, bundles=bundles, request=request)
    assert result["status"] == "COMPLETED"
    assert adapter.runs == 0
    assert result["plan"]["records"][0]["reused"] is True
    assert result["checkpointBundles"]["durableTransport"] == "not_connected"
    assert result["sideEffects"]["remoteWrites"] is False


def test_corruption_rejected_before_restore_or_adapter_run(tmp_path):
    bundles = exported(tmp_path)
    data, digest = bundles["core_collection"]
    adapter = Adapter()
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "second", adapter=adapter, bundles={"core_collection": (data + b"x", digest)})
    assert adapter.runs == 0
    assert not (tmp_path / "second/state/stages").exists()


def test_adapter_rejection_never_commits_completion(tmp_path):
    bundles = exported(tmp_path)
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "second", adapter=Adapter(reject=True), bundles=bundles)
    assert not (tmp_path / "second/state/checkpoints.json").exists()
    assert not (tmp_path / "second/result.json").exists()


def test_lease_loss_during_restore_never_commits_completion(tmp_path):
    bundles = exported(tmp_path)
    with pytest.raises(RuntimeError, match="lease_lost"):
        run(tmp_path / "second", guard=Guard(fail_at=2), bundles=bundles)
    assert not (tmp_path / "second/state/checkpoints.json").exists()
    # Partial work is not silently merged or overwritten on a retry.
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "second", bundles=bundles)


def test_restore_refuses_existing_directory_and_local_state(tmp_path):
    bundles = exported(tmp_path)
    run(tmp_path / "second")
    before = (tmp_path / "second/state/checkpoints.json").read_bytes()
    with pytest.raises(PipelineRunnerError, match="local_state_conflict"):
        run(tmp_path / "second", bundles=bundles)
    assert before == (tmp_path / "second/state/checkpoints.json").read_bytes()


def test_changed_stage_input_and_symlink_fail_closed(tmp_path):
    bundles = exported(tmp_path)
    request = payload()
    request["stageConfigs"]["core_collection"]["parserVersion"] = "2"
    request["stageInputs"]["core_collection"] = canonical_sha256(request["stageConfigs"]["core_collection"])
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "changed", bundles=bundles, request=request)
    (tmp_path / "linked/state").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked/state/stages").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "linked", bundles=bundles)
    assert not list(outside.iterdir())


def test_bundle_operations_without_authority_rejected(tmp_path):
    with pytest.raises(PipelineRunnerError, match="bundle_operations_require"):
        run_pipeline_request(payload(), state_dir=tmp_path / "state", output_path=tmp_path / "out.json",
                             producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
                             now=datetime(2026, 10, 1, tzinfo=timezone.utc), stage_adapters={"core_collection": Adapter()},
                             authorization_verifier=lambda context: True, export_bundles=True)


def test_legacy_local_checkpoint_cannot_be_exported_without_execution_binding(tmp_path):
    run(tmp_path / "first", export=False)
    path = tmp_path / "first/state/checkpoints.json"
    value = json.loads(path.read_text())
    checkpoint = value["checkpoints"][0]
    checkpoint["evidence"].pop("runnerBinding")
    checkpoint["checkpointSha256"] = canonical_sha256({k: v for k, v in checkpoint.items() if k != "checkpointSha256"})
    path.write_text(json.dumps(value))
    with pytest.raises(PipelineRunnerError, match="bundle_export_execution_binding_missing"):
        run(tmp_path / "first")


@pytest.mark.parametrize("field,value", [("dependencyLockSha256", "f" * 64), ("mode", "recovery"), ("recoveryId", "other")])
def test_execution_binding_checked_independently_of_valid_hashes(tmp_path, field, value):
    bundles = exported(tmp_path)
    content = json.loads(bundles["core_collection"][0])
    checkpoint = content["checkpoint"]
    checkpoint["evidence"]["runnerBinding"][field] = value
    checkpoint["checkpointSha256"] = canonical_sha256({k: v for k, v in checkpoint.items() if k != "checkpointSha256"})
    content["bundleManifestSha256"] = canonical_sha256({k: v for k, v in content.items() if k != "bundleManifestSha256"})
    data = json.dumps(content).encode()
    with pytest.raises(PipelineRunnerError, match="resume_bundle"):
        run(tmp_path / "second", bundles={"core_collection": (data, hashlib.sha256(data).hexdigest())})
    assert not (tmp_path / "second/state/stages").exists()
