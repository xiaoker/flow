"""Real core adapters with fixture authority; not a production canary."""
from dataclasses import replace
import json
import hashlib

import pytest

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_recovery_scope import validate_recovery_dashboard_adapters
from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
from trading_desk.pipeline_verify_adapter import CoreReadModelVerifyAdapter
from trading_desk.pipeline_runner import run_pipeline_request, PipelineRunnerError
from trading_desk.read_models import canonical_json_bytes
from test_pipeline_build_adapter import inputs
from test_pipeline_bundle_resume import payload, ROOT, LOCK, Adapter, Guard
from test_pipeline_recovery_scope import fixture, NOW
from test_pipeline_recovery_lease import Session
from test_pipeline_lease import Clock


def setup(tmp_path, publication_status="confirmed"):
    build, args, snapshot = inputs(tmp_path)
    snapshot["publication"] = {"status": publication_status}
    build.snapshot_path.write_bytes(canonical_json_bytes(snapshot))
    args["config"]["snapshotSha256"] = hashlib.sha256(build.snapshot_path.read_bytes()).hexdigest()
    args["input_sha256"] = canonical_sha256(args["config"])
    plan, context = fixture(("dashboard",))
    plan.pop("planHash")
    plan["dataDates"] = [snapshot["asOf"]]
    plan["planHash"] = canonical_sha256(plan)
    request = payload()
    request.update(mode="recovery", recovery={"recoveryId": context.recovery_id, "planSha256": plan["planHash"]},
        authorization={"kind": "approved-recovery", "verified": True, "planSha256": plan["planHash"]})
    request["identity"]["targetDate"] = snapshot["asOf"]
    request["lease"].update(owner=context.lease_owner, callbackOwner=context.lease_owner)
    context = replace(context, identity=replace(context.identity, target_date=snapshot["asOf"]),
        recovery_plan_sha256=plan["planHash"])
    clock = Clock()

    class ExactDateSession(Session):
        def request(self, *a, **kw):
            response = super().request(*a, **kw)
            value = response.payload
            if "pipeline" in value:
                value["pipeline"]["targetDate"] = snapshot["asOf"]
            if "targetDate" in value:
                value["targetDate"] = snapshot["asOf"]
            return response

    session = ExactDateSession(clock, plan)
    guard = RecoveryPipelineLease(approved_plan=plan, stages=["build", "verify"],
        base_url="https://flow.xiaoker.com", ingest_key="fixture", access_client_id="fixture",
        access_client_secret="fixture", expected_attempt_count=1, session=session,
        now=clock.now, monotonic=clock.monotonic)
    return build, args, plan, context, request, guard, session


def test_real_recovery_core_build_verify_and_resume(tmp_path):
    build, args, plan, context, request, guard, session = setup(tmp_path)
    request.update(stages=["build"], stageInputs={"build": args["input_sha256"]}, stageConfigs={"build": args["config"]})
    common = dict(producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT,
        now=NOW, lease_guard=guard, approved_recovery_plan=plan)
    first = run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
        stage_adapters={"build": build}, **common)
    assert first["status"] == "COMPLETED"
    # Locate the runner-owned workspace from its bounded local tree.
    receipts = list((tmp_path / "state").rglob("adapter-receipt.json"))
    assert len(receipts) == 1
    build_workspace = receipts[0].parent
    manifest = json.loads((build_workspace / "manifest.json").read_bytes())
    assert manifest["slot"] == "dashboard"
    assert not ({"pressure", "stock_search"} & set(manifest["artifacts"]))
    build.snapshot_path.unlink()
    def fresh_guard():
        return RecoveryPipelineLease(approved_plan=plan, stages=["build", "verify"],
            base_url="https://flow.xiaoker.com", ingest_key="fixture", access_client_id="fixture",
            access_client_secret="fixture", expected_attempt_count=1, session=session,
            now=Clock().now, monotonic=Clock().monotonic)
    common["lease_guard"] = fresh_guard()
    again = run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
        stage_adapters={"build": build}, **common)
    assert again["plan"]["records"][0]["reused"] is True
    config = {"buildInputSha256": args["input_sha256"],
        "buildOutputSha256": json.loads(receipts[0].read_bytes())["stageResult"]["outputSha256"]}
    request.update(stages=["verify"], stageInputs={"verify": canonical_sha256(config)}, stageConfigs={"verify": config})
    common["lease_guard"] = fresh_guard()
    verified = run_pipeline_request(request, state_dir=tmp_path / "verified", output_path=tmp_path / "verified.json",
        stage_adapters={"verify": CoreReadModelVerifyAdapter(build_workspace)}, **common)
    assert verified["status"] == "COMPLETED"
    assert all(call[0] == "GET" for call in session.calls)


@pytest.mark.parametrize("stage", ["build", "verify"])
def test_arbitrary_dashboard_adapter_rejected_before_io(tmp_path, stage):
    _, args, plan, _, request, guard, session = setup(tmp_path)
    adapter = Adapter()
    request.update(stages=[stage], stageInputs={stage: args["input_sha256"]}, stageConfigs={stage: args["config"]})
    with pytest.raises(PipelineRunnerError, match="recovery_scope_rejected"):
        run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
            producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=NOW,
            stage_adapters={stage: adapter}, lease_guard=guard, approved_recovery_plan=plan)
    assert not session.calls and adapter.runs == 0
    assert not (tmp_path / "state").exists()


def test_exact_adapter_still_requires_recovery_guard(tmp_path):
    build, _, plan, context, _, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="adapter_binding_required"):
        validate_recovery_dashboard_adapters({"build": build}, Guard(), plan, context, ["build"], now=NOW)


def test_subclass_cannot_replace_core_builder(tmp_path):
    from trading_desk.pipeline_build_adapter import CoreReadModelBuildAdapter
    build, _, plan, context, _, guard, session = setup(tmp_path)
    class Replacement(CoreReadModelBuildAdapter):
        pass
    with pytest.raises(ValueError, match="adapter_binding_required"):
        validate_recovery_dashboard_adapters({"build": Replacement(build.snapshot_path)},
            guard, plan, context, ["build"], now=NOW)
    assert not session.calls


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_recovery_mapping_does_not_bypass_confirmed_publication(tmp_path, status):
    from trading_desk.pipeline_adapters import PipelineAdapterError
    build, args, _, context, _, _, _ = setup(tmp_path, publication_status=status)
    args["context"] = context
    result = build.run(**args)
    config = {"buildInputSha256": args["input_sha256"], "buildOutputSha256": result["outputSha256"]}
    output = tmp_path / "verify"
    with pytest.raises(PipelineAdapterError, match="verify_publication_not_confirmed"):
        CoreReadModelVerifyAdapter(args["workspace"]).run(context=context, config=config,
            input_sha256=canonical_sha256(config), workspace=output)
    assert not output.exists()
