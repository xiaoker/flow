"""Bounded cross-host locator fixtures, not production acceptance."""
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace

import pytest

from trading_desk.pipeline_checkpoint_handoff import build_checkpoint_handoff, verify_checkpoint_handoff, handoff_input_sha256
from trading_desk.pipeline_runner import run_pipeline_request, PipelineRunnerError, build_execution_context
from test_pipeline_bundle_resume import payload, Guard, Adapter, ROOT, LOCK
from test_pipeline_checkpoint_transport import Session, transport


def execute(root, request=None, **options):
    adapter = options.pop("adapter", Adapter())
    return run_pipeline_request(request or payload(), state_dir=root / "state", output_path=root / "result.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc), stage_adapters={"core_collection": adapter},
        lease_guard=Guard(), **options)


def test_exact_handoff_transfers_two_hosts_with_new_owner_without_source_rerun(tmp_path):
    remote = Session()
    first = execute(tmp_path / "a", export_bundles=True, export_handoff=True, checkpoint_transport=transport(remote))
    locator = first["checkpointHandoff"]
    assert json.loads((tmp_path / "a/state/handoff.json").read_bytes()) == locator
    request = payload(); request["lease"].update(owner="host-b", callbackOwner="host-b", attemptCount=2)
    adapter = Adapter()
    second = execute(tmp_path / "b", request, adapter=adapter, checkpoint_transport=transport(remote),
                     resume_handoff_sha256=locator["sha256"])
    assert second["status"] == "COMPLETED" and adapter.runs == 0
    assert second["checkpointBundles"]["restoredStages"] == ["core_collection"]
    assert [call[0] for call in remote.calls] == ["POST", "POST", "GET", "GET"]
    assert not second["sideEffects"]["remoteWrites"]


def test_missing_or_corrupt_handoff_fails_before_source_or_restore(tmp_path):
    remote = Session(); adapter = Adapter()
    with pytest.raises(PipelineRunnerError, match="read_rejected"):
        execute(tmp_path / "missing", adapter=adapter, checkpoint_transport=transport(remote), resume_handoff_sha256="f" * 64)
    assert adapter.runs == 0
    first = execute(tmp_path / "a", export_bundles=True, export_handoff=True, checkpoint_transport=transport(remote))
    remote.corrupt = True
    with pytest.raises(PipelineRunnerError, match="bundle_invalid"):
        execute(tmp_path / "bad", adapter=adapter, checkpoint_transport=transport(remote), resume_handoff_sha256=first["checkpointHandoff"]["sha256"])
    assert adapter.runs == 0 and not (tmp_path / "bad/state/stages").exists()


def contract(tmp_path):
    remote = Session(); report = execute(tmp_path, export_bundles=True, export_handoff=True, checkpoint_transport=transport(remote))
    request = payload()
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    data = remote.objects[report["checkpointHandoff"]["key"]]
    return context, request, data


@pytest.mark.parametrize("mutation", ["key", "size", "hash", "identity", "lock", "unknown", "empty", "bool"])
def test_handoff_contract_rejects_conflicts_even_with_recomputed_digest(tmp_path, mutation):
    context, request, data = contract(tmp_path)
    value = json.loads(data); ref = value["references"]["core_collection"]
    if mutation == "key": ref["key"] = "other-bucket/secret"
    elif mutation == "size": ref["bytes"] = 1024 * 1024 + 1
    elif mutation == "hash": ref["sha256"] = "invalid"
    elif mutation == "identity": value["plan"]["identity"]["targetDate"] = "2026-09-28"
    elif mutation == "lock": value["plan"]["dependencyLockSha256"] = "f" * 64
    elif mutation == "unknown": value["references"]["other"] = ref
    elif mutation == "empty": value["references"] = {}
    else: value["schemaVersion"] = True
    data = json.dumps(value).encode()
    with pytest.raises(ValueError, match="contract_mismatch"):
        verify_checkpoint_handoff(data, sha256=hashlib.sha256(data).hexdigest(), context=context,
            input_sha256=handoff_input_sha256(context, request["stages"], request["stageInputs"]))


def test_deterministic_locator_and_recovery_never_accepted(tmp_path):
    context, request, data = contract(tmp_path)
    value = json.loads(data)
    exports = {k: {"remote": v} for k, v in value["references"].items()}
    assert build_checkpoint_handoff(context, request["stages"], request["stageInputs"], exports)[0] == data
    with pytest.raises(ValueError, match="plan_invalid"):
        handoff_input_sha256(replace(context, mode="recovery"), request["stages"], request["stageInputs"])


def test_ambiguous_or_unbacked_handoff_configuration_has_no_http(tmp_path):
    remote = Session()
    with pytest.raises(PipelineRunnerError, match="requires_durable"):
        execute(tmp_path / "a", export_handoff=True)
    with pytest.raises(PipelineRunnerError, match="ambiguous"):
        execute(tmp_path / "b", checkpoint_transport=transport(remote), resume_handoff_sha256="f" * 64, resume_references={})
    assert not remote.calls


def test_confirmed_prefix_survives_later_stage_crash_and_only_missing_stage_runs(tmp_path):
    request = payload(); request["stages"].append("normalize")
    request["stageInputs"]["normalize"] = request["stageInputs"]["core_collection"]
    request["stageConfigs"]["normalize"] = dict(request["stageConfigs"]["core_collection"])
    remote = Session()
    class Interrupted(Adapter):
        def run(self, **kwargs):
            raise OSError("fixture power loss")
    def run_at(root, adapters, **options):
        return run_pipeline_request(request, state_dir=root / "state", output_path=root / "result.json",
            producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc), stage_adapters=adapters,
            lease_guard=Guard(), checkpoint_transport=transport(remote), **options)
    with pytest.raises(PipelineRunnerError, match="stage_adapter_failed"):
        run_at(tmp_path / "a", {"core_collection": Adapter(), "normalize": Interrupted()},
               export_bundles=True, export_handoff=True)
    locator = json.loads((tmp_path / "a/state/handoff.json").read_bytes())
    core, normalized = Adapter(), Adapter()
    result = run_at(tmp_path / "b", {"core_collection": core, "normalize": normalized},
                    resume_handoff_sha256=locator["sha256"])
    assert result["status"] == "COMPLETED" and core.runs == 0 and normalized.runs == 1


def test_declared_bundle_size_conflict_rejected_before_restore(tmp_path):
    remote = Session()
    first = execute(tmp_path / "a", export_bundles=True, export_handoff=True, checkpoint_transport=transport(remote))
    key = first["checkpointHandoff"]["key"]
    value = json.loads(remote.objects[key]); value["references"]["core_collection"]["bytes"] += 1
    data = json.dumps(value).encode(); digest = hashlib.sha256(data).hexdigest()
    remote.objects[key.replace(first["checkpointHandoff"]["sha256"], digest)] = data
    adapter = Adapter()
    with pytest.raises(PipelineRunnerError, match="bundle_size_mismatch"):
        execute(tmp_path / "b", adapter=adapter, checkpoint_transport=transport(remote), resume_handoff_sha256=digest)
    assert adapter.runs == 0 and not (tmp_path / "b/state/stages").exists()


def test_handoff_unknown_budget_never_makes_remote_read_or_runs_source(tmp_path):
    remote = Session(); adapter = Adapter()
    with pytest.raises(PipelineRunnerError, match="resource_allowance_required"):
        execute(tmp_path, adapter=adapter, checkpoint_transport=transport(remote, budget_ledger=None),
                resume_handoff_sha256="f" * 64)
    assert not remote.calls and adapter.runs == 0
