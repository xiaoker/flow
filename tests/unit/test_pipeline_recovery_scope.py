from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trading_desk.pipeline_recovery_scope import bind_recovery_runner_scope
from trading_desk.recovery_execution import build_recovery_plan
from test_pipeline_lease import context


NOW = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)


def fixture(stages=("core_collection",)):
    original = context()
    plan = build_recovery_plan(campaign_id="fixture", recovery_id="fixture-recovery",
        run_key=original.identity.run_key, data_dates=[original.identity.target_date],
        required_stages=stages, artifact_root=".recovery/fixture", state_dir=".recovery/fixture/state",
        max_dates=1, max_requests=20, deadline="2026-09-30T10:00:00.000Z",
        budget_manifest={"fixture": True})
    bound = replace(original, mode="recovery", authorization_kind="approved-recovery",
        recovery_id="fixture-recovery", recovery_plan_sha256=plan["planHash"])
    return plan, bound


def test_exact_scope_is_not_live_permission():
    plan, bound = fixture()
    evidence = bind_recovery_runner_scope(plan, bound, ["core_collection"], now=NOW)
    assert evidence["liveAuthorityVerified"] is False
    assert evidence["targetDate"] == "2026-09-29"


@pytest.mark.parametrize("field,value", [("recovery_id", "other"), ("recovery_plan_sha256", "0" * 64),
    ("mode", "ordinary"), ("authorization_kind", "existing-admin")])
def test_identity_and_hash_cannot_be_rebound(field, value):
    plan, bound = fixture()
    with pytest.raises(ValueError):
        bind_recovery_runner_scope(plan, replace(bound, **{field: value}), ["core_collection"], now=NOW)


@pytest.mark.parametrize("stages", [["build"], ["normalize"], ["verify"], ["verify_visible"],
    ["publish"], ["core_collection", "core_collection"], []])
def test_scope_does_not_widen_or_invent_domain_mapping(stages):
    plan, bound = fixture()
    with pytest.raises(ValueError):
        bind_recovery_runner_scope(plan, bound, stages, now=NOW)


def test_dashboard_approval_is_not_generic_build_permission():
    plan, bound = fixture(("dashboard",))
    with pytest.raises(ValueError, match="mapping_unproven"):
        bind_recovery_runner_scope(plan, bound, ["build"], now=NOW)


def test_expired_plan_and_wrong_target_rejected():
    plan, bound = fixture()
    with pytest.raises(ValueError, match="expired"):
        bind_recovery_runner_scope(plan, bound, ["core_collection"], now=datetime(2026, 9, 30, 10, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="identity"):
        bind_recovery_runner_scope(plan, replace(bound, identity=replace(bound.identity, target_date="2026-09-28")),
            ["core_collection"], now=NOW)


@pytest.mark.parametrize("missing", [True, False])
def test_runner_rejects_scope_before_authorization_or_adapter_effects(tmp_path, missing):
    from test_pipeline_bundle_resume import payload, Adapter, ROOT, LOCK
    from trading_desk.pipeline_runner import run_pipeline_request, PipelineRunnerError
    plan, bound = fixture(("dashboard",))
    request = payload()
    request["mode"] = "recovery"
    request["authorization"] = {"kind": "approved-recovery", "verified": True, "planSha256": plan["planHash"]}
    request["recovery"] = {"recoveryId": bound.recovery_id, "planSha256": plan["planHash"]}
    adapter = Adapter()
    calls = []
    with pytest.raises(PipelineRunnerError, match="recovery_scope"):
        run_pipeline_request(request, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
            producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=NOW,
            stage_adapters={"core_collection": adapter}, authorization_verifier=lambda context: calls.append(context) or True,
            approved_recovery_plan=None if missing else plan)
    assert not calls
    assert adapter.runs == 0
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "result.json").exists()
