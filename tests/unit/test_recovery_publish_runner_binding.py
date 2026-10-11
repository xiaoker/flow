"""Wiring and early rejection tests, not live recovery acceptance."""
from dataclasses import replace
from pathlib import Path
import pytest

from trading_desk.pipeline_recovery_scope import validate_recovery_publication_adapter
from trading_desk.pipeline_publish_adapter import CoreCandidatePublishAdapter
from trading_desk.pipeline_runner import run_pipeline_request, PipelineRunnerError
from trading_desk.pipeline_application import canonical_sha256
from test_recovery_candidate_transport import setup
from test_pipeline_recovery_scope import NOW
from test_pipeline_bundle_resume import payload, ROOT, LOCK, Adapter, Guard

def test_exact_recovery_publication_wiring_is_pure():
    transport, source, remote, budget, _ = setup()
    adapter = CoreCandidatePublishAdapter(Path("unused-fixture-source"), transport, source["context"])
    assert validate_recovery_publication_adapter(adapter, transport._guard, transport._plan,
        source["context"], now=NOW) is True
    assert not remote.calls and not budget.calls

@pytest.mark.parametrize("wrong", ["guard", "context", "ledger_guard", "task", "adapter"])
def test_wrong_binding_cannot_grant_publication(wrong):
    transport, source, remote, budget, _ = setup()
    guard = transport._guard
    adapter = CoreCandidatePublishAdapter(Path("unused"), transport, source["context"])
    if wrong == "guard": guard = Guard()
    if wrong == "context": adapter = replace(adapter, execution_context=replace(source["context"], lease_owner="other"))
    if wrong == "ledger_guard": transport._ledger._guard = Guard()
    if wrong == "task": transport._task = "other"
    if wrong == "adapter": adapter = Adapter()
    with pytest.raises((ValueError, PipelineRunnerError)):
        validate_recovery_publication_adapter(adapter, guard, transport._plan, source["context"], now=NOW)
    assert not remote.calls and not budget.calls

def test_runner_rejects_untrusted_recovery_publish_before_authorization_and_files(tmp_path):
    transport, source, _, _, _ = setup(); context = source["context"]
    request = payload(); config = {"fixture": True}
    request.update(mode="recovery", stages=["publish"], stageInputs={"publish": canonical_sha256(config)},
        stageConfigs={"publish": config}, recovery={"recoveryId": context.recovery_id, "planSha256": context.recovery_plan_sha256},
        authorization={"kind": "approved-recovery", "verified": True, "planSha256": context.recovery_plan_sha256})
    guard = Guard(); adapter = Adapter()
    with pytest.raises(PipelineRunnerError, match="recovery_scope_rejected"):
        run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
            producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=NOW,
            stage_adapters={"publish": adapter}, lease_guard=guard, approved_recovery_plan=transport._plan)
    assert guard.calls == 0 and adapter.runs == 0
    assert not (tmp_path / "state").exists()
