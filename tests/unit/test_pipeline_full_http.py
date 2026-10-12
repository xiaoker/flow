"""Actual runner/adapters/clients against built Worker and local SQLite/R2.

Provider usage remains UNKNOWN. No production requests or canary evidence.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
import requests

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_runner import (run_pipeline_request, PipelineRunnerError,
    dependency_lock_digest, build_execution_context, runner_budget_input_sha256)
from trading_desk.pipeline_build_adapter import CoreReadModelBuildAdapter
from trading_desk.pipeline_verify_adapter import CoreReadModelVerifyAdapter
from trading_desk.pipeline_publish_adapter import CoreCandidatePublishAdapter
from trading_desk.pipeline_candidate_transport import CandidatePublicationTransport
from trading_desk.pipeline_candidate_transport import RecoveryCandidatePublicationTransport
from trading_desk.pipeline_checkpoint_transport import CheckpointObjectTransport, RecoveryCheckpointObjectTransport, MAX_OBJECT_BYTES
from trading_desk.pipeline_lease import OrdinaryPipelineLease
from trading_desk.pipeline_recovery_lease import RecoveryPipelineLease
from trading_desk.pipeline_checkpoint_preflight import fresh_candidate_request_envelope, FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile
from trading_desk.pipeline_checkpoint_preflight import ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile
from trading_desk.pipeline_publication_intent import PublicationIntentStore
from trading_desk.resource_budget_remote import SharedBudgetLedger, RecoverySharedBudgetLedger, DefinitiveBudgetRejection
from trading_desk.recovery_execution import build_recovery_plan
from trading_desk.resource_budget_store import RemoteBudgetOperationStore
from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS
from test_pipeline_verify_adapter import setup
from test_pipeline_bundle_resume import payload, ROOT, LOCK

NOW = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)


def resume_child(config):
    """Separate-process test entry; production-same clients, fixture data only."""
    request, plan = config["request"], config["plan"]
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=NOW)
    digest = runner_budget_input_sha256(context, request["stages"], request["stageInputs"])
    profile_type = ResumeRecoveryCandidateRequestProfile if plan else ResumeCandidateRequestProfile
    profile = profile_type(digest, len(config["manifest"]["artifacts"]),
        **({"recovery_id": plan["recoveryId"], "plan_sha256": plan["planHash"]} if plan else {}))
    common = dict(base_url=config["url"], ingest_key="fixture-only", access_client_id="fixture-id",
        access_client_secret="fixture-secret", allow_loopback_fixture=True)
    guard_options = dict(**common, expected_attempt_count=2, now=lambda: NOW, request_profile=profile)
    guard = RecoveryPipelineLease(**guard_options, approved_plan=plan, stages=request["stages"]) if plan else OrdinaryPipelineLease(**guard_options)
    store = RemoteBudgetOperationStore(config["journal"], task_id=config["task"], input_sha256=digest)
    budget_options = dict(**common, lease_guard=guard, run_key=context.identity.run_key,
        lease_owner=context.lease_owner, attempt_count=2, target_date=context.identity.target_date,
        server_fenced_checks=True, task_input_sha256=digest, operation_store=store, request_profile=profile)
    ledger = RecoverySharedBudgetLedger(**budget_options, approved_plan=plan, context=context, stages=request["stages"], now=NOW) if plan else SharedBudgetLedger(**budget_options)
    execution = dict(runKey=context.identity.run_key, targetDate=context.identity.target_date,
        producerBuildSha="a" * 40, leaseOwner=context.lease_owner, attemptCount=2)
    if plan:
        from trading_desk.recovery_execution import plan_bytes
        execution.update(recoveryId=plan["recoveryId"], planSha256=plan["planHash"], scopeSha256=plan_bytes(plan)[1])
    from pathlib import Path
    root = Path(config["root"])
    intent = PublicationIntentStore.initialize(root / "new-owner-intent.sqlite", execution=execution,
        release_id=config["manifest"]["releaseId"], manifest_sha256=config["manifest"]["manifestSha256"])
    costs = dict.fromkeys(RESOURCE_DIMENSIONS, 100)
    costs["r2Bytes"] = MAX_OBJECT_BYTES * 2
    transport_options = dict(**common, lease_guard=guard, budget_ledger=ledger, budget_task_id=config["task"],
        request_costs={method: BudgetVector.estimated(costs) for method in ("GET", "POST")})
    candidate = RecoveryCandidatePublicationTransport(**transport_options, intent_store=intent, approved_plan=plan, now=lambda: NOW) if plan else CandidatePublicationTransport(**transport_options, intent_store=intent)
    checkpoints = RecoveryCheckpointObjectTransport(**transport_options, approved_plan=plan, stages=request["stages"], now=lambda: NOW) if plan else CheckpointObjectTransport(**transport_options)
    state = root / "second/state"
    workspaces = {stage: state / "stages" / stage / request["stageInputs"][stage] for stage in request["stages"]}
    adapters = {"build": CoreReadModelBuildAdapter(Path(config["source"])),
        "verify": CoreReadModelVerifyAdapter(workspaces["build"]),
        "publish": CoreCandidatePublishAdapter(workspaces["verify"], candidate, context, restore_completed_intents=True)}
    report = run_pipeline_request(request, state_dir=state, output_path=root / "second/result.json",
        producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=NOW,
        stage_adapters=adapters, lease_guard=guard, budget_ledger=ledger,
        task_cost_upper=BudgetVector.estimated(config["upper"]),
        control_request_cost_upper={name: BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1)) for name in ("lease", "budget")},
        runtime_cost_upper=BudgetVector.estimated({**dict.fromkeys(RESOURCE_DIMENSIONS, 1), "actionsMinutes": 10}),
        stage_cost_upper={stage: BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1)) for stage in adapters},
        resource_measurement=lambda kind, stage: BudgetVector.measured(dict.fromkeys(RESOURCE_DIMENSIONS, 1),
            measured_at=NOW.isoformat(), scope="fixture_stage") if kind == "stage" else BudgetVector.unknown(),
        export_bundles=True, export_handoff=True, checkpoint_transport=checkpoints,
        resume_handoff_sha256=config["handoff"], **({"approved_recovery_plan": plan} if plan else {}))
    print(json.dumps(report))


@pytest.mark.parametrize("probe", ["legacy", "normal", "revision_conflict", "lost_candidate_reply",
    "understated_control", "resume_scope", "wrong_manifest_count", "existing_hold",
    "wrong_source_hash", "wrong_data_date", "blocked_qa", "crash_resume", "unknown_takeover", "unknown_stage_usage",
    "understated_r2", "unknown_control_cost", "authority_lost", "crash_after_verify", "known_task_usage", "crash_after_publish"])
@pytest.mark.parametrize("recovery", [False, True])
def test_real_candidate_checkpoint_chain_preserves_hold_at_legacy_lease_ceiling(tmp_path, probe, recovery, monkeypatch):
    diagnostic_probe = probe != "legacy"
    verify, reference = setup(tmp_path, publication_status="blocked" if probe == "blocked_qa" else "confirmed")
    verified = {"outputSha256": "a" * 64} if probe == "blocked_qa" else verify.run(**reference)
    manifest = json.loads((verify.source_dir / "manifest.json").read_bytes())
    built = json.loads((verify.source_dir / "adapter-receipt.json").read_bytes())["stageResult"]
    request = payload()
    request["identity"] = reference["context"].identity.as_dict()
    request["stages"] = ["build", "verify", "publish"]
    source_path = tmp_path / "source.json"
    import hashlib
    configs = {
        "build": {"snapshotSha256": hashlib.sha256(source_path.read_bytes()).hexdigest()},
        "verify": {"buildInputSha256": canonical_sha256({"snapshotSha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}),
                   "buildOutputSha256": built["outputSha256"]},
        "publish": {"verifyInputSha256": reference["input_sha256"], "verifyOutputSha256": verified["outputSha256"]},
    }
    if probe == "wrong_source_hash":
        configs["build"]["snapshotSha256"] = "d" * 64
    if probe == "wrong_data_date":
        from datetime import date, timedelta
        request["identity"]["targetDate"] = (date.fromisoformat(request["identity"]["targetDate"]) - timedelta(days=1)).isoformat()
    request["stageConfigs"] = configs
    request["stageInputs"] = {stage: canonical_sha256(config) for stage, config in configs.items()}
    request["dependencyLockSha256"] = dependency_lock_digest([LOCK], root=ROOT)["sha256"]
    upper = dict.fromkeys(RESOURCE_DIMENSIONS, 10_000)
    # Functional fixture allowance includes the full two-attempt envelope;
    # it is not evidence of provider capacity or physical billing usage.
    upper["r2Bytes"] = 200_000_000
    plan = None
    if recovery:
        plan = build_recovery_plan(campaign_id="full-fixture", recovery_id="full-fixture-recovery",
            run_key=request["identity"]["runKey"], data_dates=[request["identity"]["targetDate"]],
            required_stages=["dashboard", "publish"], artifact_root=".recovery/fixture",
            state_dir=".recovery/fixture/state", max_dates=1, max_requests=1000 if probe in {"crash_resume", "crash_after_verify", "crash_after_publish", "unknown_takeover"} else 600,
            deadline="2026-10-03T03:00:00Z", budget_manifest={"runnerBudgetContract": "flow-shared-budget-v1",
                "taskCostUpperBound": BudgetVector.estimated(upper).as_dict()})
        request["mode"] = "recovery"
        request["authorization"] = {"kind": "approved-recovery", "verified": True, "planSha256": plan["planHash"]}
        request["recovery"] = {"recoveryId": plan["recoveryId"], "planSha256": plan["planHash"]}
    context = build_execution_context(request, producer_build_sha="a" * 40,
        dependency_lock_sha256=request["dependencyLockSha256"], now=NOW)
    fixture_profile = (ResumeRecoveryCandidateRequestProfile("a" * 64, len(manifest["artifacts"]),
        recovery_id=plan["recoveryId"], plan_sha256=plan["planHash"]) if recovery else
        ResumeCandidateRequestProfile("a" * 64, len(manifest["artifacts"])))
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/full-pipeline-server.mjs")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "FLOW_FULL_FIXTURE_RUN_KEY": context.identity.run_key,
             # Two-attempt derived ceiling + bounded fixture-only stats/fault/
             # claim calls; this is not a production or provider limit.
             "FLOW_FULL_FIXTURE_REQUEST_LIMIT": str(fixture_profile.envelope()["taskWorkerRequestsUpper"] + 16),
             "FLOW_FULL_FIXTURE_TARGET": context.identity.target_date, "FLOW_FULL_FIXTURE_OWNER": context.lease_owner,
             "FLOW_FULL_FIXTURE_REVISION_CONFLICT": "1" if probe == "revision_conflict" else "0",
             **({"FLOW_FULL_FIXTURE_PLAN": json.dumps(plan)} if plan else {})})
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), process.stderr.read() if process.poll() is not None else "fixture startup failed"
        url = f"http://127.0.0.1:{port}"
        common = dict(base_url=url, ingest_key="fixture-only", access_client_id="fixture-id",
            access_client_secret="fixture-secret", allow_loopback_fixture=True)
        task = f"{context.identity.run_key}/{context.identity.target_date}/flow-budget-task-v2"
        if recovery:
            task = f"recovery/{plan['recoveryId']}/{plan['planHash']}/{task}"
        digest = runner_budget_input_sha256(context, request["stages"], request["stageInputs"])
        profile_type = FreshRecoveryCandidateRequestProfile if recovery else FreshCandidateRequestProfile
        profile_args = dict(recovery_id=plan["recoveryId"], plan_sha256=plan["planHash"]) if recovery else {}
        profile = profile_type(digest, len(manifest["artifacts"]) - int(probe == "wrong_manifest_count"), **profile_args) if diagnostic_probe else None
        def make_guard():
            options = dict(**common, expected_attempt_count=1, now=lambda: NOW, request_profile=profile)
            return RecoveryPipelineLease(**options, approved_plan=plan, stages=request["stages"]) if recovery else OrdinaryPipelineLease(**options)
        guard = make_guard()
        store = RemoteBudgetOperationStore.initialize(tmp_path / "operation.sqlite", task_id=task, input_sha256=digest)
        def make_ledger(lease):
            options = dict(**common, lease_guard=lease, run_key=context.identity.run_key,
                lease_owner=context.lease_owner, attempt_count=1, target_date=context.identity.target_date,
                server_fenced_checks=True, task_input_sha256=digest, operation_store=store, request_profile=profile)
            return RecoverySharedBudgetLedger(**options, approved_plan=plan, context=context, stages=request["stages"], now=NOW) if recovery else SharedBudgetLedger(**options)
        ledger = make_ledger(guard)
        envelope = fresh_candidate_request_envelope(stage_count=3, artifact_count=len(manifest["artifacts"]),
            renewal_requests=0, revision_conflicts=int(probe == "revision_conflict"),
            checkpoint_readbacks=0, candidate_reconciliations=0)
        if recovery:
            envelope["normalRequests"]["lease"] *= 2
            envelope["requestsUpper"]["lease"] *= 2
        costs = dict.fromkeys(RESOURCE_DIMENSIONS, 100)
        costs["r2Bytes"] = MAX_OBJECT_BYTES * 2
        effect_cost = BudgetVector.estimated(costs)
        execution = {"runKey": context.identity.run_key, "targetDate": context.identity.target_date,
            "producerBuildSha": context.identity.producer_build_sha, "leaseOwner": context.lease_owner, "attemptCount": 1}
        if recovery:
            from trading_desk.recovery_execution import plan_bytes
            execution.update(recoveryId=plan["recoveryId"], planSha256=plan["planHash"], scopeSha256=plan_bytes(plan)[1])
        intent = PublicationIntentStore.initialize(tmp_path / "intent.sqlite", execution=execution,
            release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])
        class LoseCommittedReply(requests.Session):
            lost = False

            def request(self, method, url, **options):
                response = super().request(method, url, **options)
                if not self.lost and method == "POST" and url.endswith("/read-model-import"):
                    assert response.status_code == 200
                    self.lost = True
                    response.close()
                    raise requests.ConnectionError("fixture committed reply discarded")
                return response

        session = LoseCommittedReply() if probe in {"lost_candidate_reply", "unknown_takeover"} else None
        candidate_type = RecoveryCandidatePublicationTransport if recovery else CandidatePublicationTransport
        checkpoint_type = RecoveryCheckpointObjectTransport if recovery else CheckpointObjectTransport
        candidate = candidate_type(**common, lease_guard=guard, budget_ledger=ledger,
            budget_task_id=task, request_costs={method: effect_cost for method in ("GET", "POST")},
            intent_store=intent, session=session, **({"approved_plan": plan, "now": lambda: NOW} if recovery else {}))
        checkpoints = checkpoint_type(**common, lease_guard=guard, budget_ledger=ledger,
            budget_task_id=task, request_costs={method: effect_cost for method in ("GET", "POST")},
            **({"approved_plan": plan, "stages": request["stages"], "now": lambda: NOW} if recovery else {}))
        state = tmp_path / "run/state"
        workspaces = {stage: state / "stages" / stage / request["stageInputs"][stage] for stage in request["stages"]}
        adapters = {"build": CoreReadModelBuildAdapter(source_path),
            "verify": CoreReadModelVerifyAdapter(workspaces["build"]),
            "publish": CoreCandidatePublishAdapter(workspaces["verify"], candidate, context)}
        if probe == "crash_after_verify":
            def interrupt_before_candidate(self, **options):
                raise SystemExit("fixture abrupt caller termination")
            # Fault at the real adapter boundary; preserve its exact class and
            # recovery binding. The new process imports the unpatched adapter.
            monkeypatch.setattr(CoreCandidatePublishAdapter, "run", interrupt_before_candidate)
        if probe == "understated_control":
            upper["workerRequests"] = profile.envelope()["workerRequestsUpper"]
        if probe == "understated_r2":
            upper["r2Bytes"] = 1
        if probe == "existing_hold":
            guard.verify(context)
            assert ledger.reserve(task, BudgetVector.estimated(upper))["decision"] == "allow"
            # A distinct client/guard attempts the fresh-only path against an
            # existing reservation. Rejection must not settle or relabel it.
            guard = make_guard()
            ledger = make_ledger(guard)
            candidate = candidate_type(**common, lease_guard=guard, budget_ledger=ledger,
                budget_task_id=task, request_costs={method: effect_cost for method in ("GET", "POST")}, intent_store=intent,
                **({"approved_plan": plan, "now": lambda: NOW} if recovery else {}))
            checkpoints = checkpoint_type(**common, lease_guard=guard, budget_ledger=ledger,
                budget_task_id=task, request_costs={method: effect_cost for method in ("GET", "POST")},
                **({"approved_plan": plan, "stages": request["stages"], "now": lambda: NOW} if recovery else {}))
            adapters["publish"] = CoreCandidatePublishAdapter(workspaces["verify"], candidate, context)
        def execute():
            def measure(kind, stage):
                if probe == "crash_after_publish" and kind == "task":
                    raise SystemExit("fixture abrupt caller termination")
                if probe == "known_task_usage" and kind == "task":
                    # Injected known test measurement exercises settlement only.
                    # No assertion represents Cloudflare/GitHub physical usage.
                    return BudgetVector.measured(upper, measured_at=NOW.isoformat(), scope="fixture_task_only")
                if probe == "authority_lost" and kind == "stage" and stage == "build":
                    endpoint = "/fixture/revoke-approval" if recovery else "/fixture/expire-lease"
                    reply = requests.post(url + endpoint, headers={"Authorization": "Bearer fixture-only"}, timeout=5)
                    assert reply.status_code == 200
                if probe == "crash_resume" and kind == "stage" and stage == "verify":
                    raise SystemExit("fixture abrupt caller termination")
                if probe == "unknown_stage_usage" and kind == "stage" and stage == "verify":
                    return BudgetVector.unknown()
                return BudgetVector.measured(dict.fromkeys(RESOURCE_DIMENSIONS, 1),
                    measured_at=NOW.isoformat(), scope="fixture_stage") if kind == "stage" else BudgetVector.unknown()
            return run_pipeline_request(request, state_dir=state, output_path=tmp_path / "run/result.json",
                producer_build_sha="a" * 40, dependency_lock_paths=[LOCK], project_root=ROOT, now=NOW,
                stage_adapters=adapters, lease_guard=guard, budget_ledger=ledger,
                task_cost_upper=BudgetVector.estimated(upper), stage_cost_upper={stage: BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1)) for stage in adapters},
                control_request_cost_upper={name: BudgetVector.estimated({**dict.fromkeys(RESOURCE_DIMENSIONS, 1),
                    **({"d1RowsRead": None} if probe == "unknown_control_cost" else {})}) for name in ("lease", "budget")},
                runtime_cost_upper=BudgetVector.estimated({**dict.fromkeys(RESOURCE_DIMENSIONS, 1), "actionsMinutes": 10}),
                resource_measurement=measure, export_bundles=True,
                    export_handoff=True, checkpoint_transport=checkpoints,
                    **({"approved_recovery_plan": plan} if recovery else {}),
                    **({"resume_references": {}} if probe == "resume_scope" else {}))
        if probe in {"crash_resume", "crash_after_verify", "crash_after_publish", "unknown_takeover"}:
            if probe in {"crash_resume", "crash_after_verify", "crash_after_publish"}:
                with pytest.raises(SystemExit, match="abrupt caller termination"):
                    execute()
                assert candidate.upload_attempts == (len(manifest["artifacts"]) if probe == "crash_after_publish" else 0)
            else:
                with pytest.raises(PipelineRunnerError, match="candidate_transport_unavailable"):
                    execute()
                assert candidate.upload_attempts == 1 and intent.has_pending()
            before = ledger.snapshot()["reservation"]
            assert before["status"] == ("UNKNOWN" if probe == "unknown_takeover" else "RUNNING")
            headers = {"Authorization": "Bearer fixture-only"}
            assert requests.post(url + "/fixture/expire-lease", headers=headers, timeout=5).status_code == 200
            new_request = json.loads(json.dumps(request))
            new_request["lease"].update(owner="second-owner", callbackOwner="second-owner", attemptCount=2)
            claim_body = {"runKey": context.identity.run_key, "leaseOwner": "second-owner"}
            claim_path = "/api/v1/admin/pipeline-window/recovery/claim" if recovery else "/api/v1/admin/pipeline-window/claim"
            if recovery:
                claim_body["recoveryId"] = plan["recoveryId"]
            else:
                claim_body.update(windowDate=context.identity.window_date, windowName="reconcile",
                    contractVersion="v1", targetDate=context.identity.target_date)
            claim = requests.post(url + claim_path, headers=headers, json=claim_body, timeout=5)
            assert claim.status_code == 200, claim.text
            assert claim.json()["attemptCount"] == 2
            handoff = json.loads((state / "handoff.json").read_bytes())["sha256"]
            source_path.unlink()  # Completed build must come from verified R2 checkpoint.
            config = dict(request=new_request, plan=plan, manifest=manifest, task=task, url=url,
                root=str(tmp_path), journal=str(tmp_path / "operation.sqlite"), source=str(source_path),
                upper=upper, handoff=handoff)
            child = subprocess.run([sys.executable, "-c",
                "import json,sys; from test_pipeline_full_http import resume_child; resume_child(json.loads(sys.argv[1]))", json.dumps(config)],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": "src:.:tests/unit"}, capture_output=True, text=True, timeout=30)
            if probe == "unknown_takeover":
                assert child.returncode != 0 and "runner_budget_hold_unresolved" in child.stderr
                final = requests.get(url + "/fixture/stats", headers=headers, timeout=5).json()
                assert final["active"] == 0 and len(final["budgetReservations"]) == 1
                reservation = final["budgetReservations"][task]
                assert reservation["status"] == "UNKNOWN"
                for field in ("cost", "authorized", "consumed"):
                    assert reservation[field] == before[field]
                assert reservation["execution"]["leaseOwner"] == "second-owner"
                assert final["http"]["/api/v1/admin/read-model-import"] == 1
                assert intent.has_pending()
                return
            assert child.returncode == 0, child.stderr
            result = json.loads(child.stdout)
            assert result["stageExecutionStatus"] == "COMPLETED"
            assert result["status"] == "UNKNOWN"
            assert result["plan"]["records"][0]["reused"] is True
            if probe in {"crash_after_verify", "crash_after_publish"}:
                assert result["plan"]["records"][1]["reused"] is True
            if probe == "crash_after_publish":
                assert result["plan"]["records"][2]["reused"] is True
            assert result["budgetLifecycle"]["taskId"] == task
            # Old actor is fenced both locally in the retained journal and remotely.
            with pytest.raises(ValueError, match="stale_owner"):
                ledger.settle(task, outcome="UNKNOWN")
            assert store.pending() is None
            with pytest.raises(PipelineRunnerError, match="identity_conflict"):
                guard.confirm()
            final = requests.get(url + "/fixture/stats", headers=headers, timeout=5).json()
            assert final["active"] == 0 and len(final["budgetReservations"]) == 1
            reservation = final["budgetReservations"][task]
            assert reservation["status"] == "UNKNOWN"
            assert reservation["cost"] == before["cost"]
            assert reservation["execution"]["leaseOwner"] == "second-owner"
            assert reservation["execution"]["attemptCount"] == 2
            assert reservation["consumed"] == dict.fromkeys(RESOURCE_DIMENSIONS, 3)
            assert final["http"]["/api/v1/admin/read-model-import"] == len(manifest["artifacts"])
            return
        elif probe == "authority_lost":
            with pytest.raises(DefinitiveBudgetRejection) as rejected:
                execute()
            assert rejected.value.code == "budget_lease_conflict"
            assert candidate.upload_attempts == 0
            assert ledger._pending is None  # Definitive rejection, not a lost reply.
        elif probe in {"wrong_source_hash", "wrong_data_date", "blocked_qa"}:
            expected = {"wrong_source_hash": "build_snapshot_hash_mismatch",
                "wrong_data_date": "build_snapshot_date_mismatch", "blocked_qa": "verify_publication_not_confirmed"}[probe]
            with pytest.raises(PipelineRunnerError, match=expected):
                execute()
            assert candidate.upload_attempts == 0
        elif probe == "existing_hold":
            with pytest.raises(PipelineRunnerError, match="runner_request_profile_requires_fresh_task"):
                execute()
            assert ledger.request_count == 1  # Read only: no adoption or settle.
        elif probe in {"understated_control", "resume_scope", "understated_r2", "unknown_control_cost"}:
            expected = {"understated_control": "runner_control_budget_understated",
                "resume_scope": "runner_request_profile_scope_invalid",
                "understated_r2": "runner_task_budget_understated",
                "unknown_control_cost": "runner_overhead_budget_unknown"}[probe]
            with pytest.raises(PipelineRunnerError, match=expected):
                execute()
            assert guard.request_count == ledger.request_count == candidate.request_count == checkpoints.request_count == 0
        elif probe == "wrong_manifest_count":
            with pytest.raises(PipelineRunnerError, match="candidate_transport_profile_artifact_mismatch"):
                execute()
            assert candidate.upload_attempts == 0
        elif probe == "lost_candidate_reply":
            with pytest.raises(PipelineRunnerError, match="candidate_transport_unavailable"):
                execute()
            assert session.lost is True
            assert candidate.upload_attempts == 1
            assert intent.has_pending() is True
            # A fresh client reconstructs the persistent unknown intent. The
            # budget UNKNOWN hold is not treated as permission to retry an effect.
            reopened = PublicationIntentStore(tmp_path / "intent.sqlite", execution=execution,
                release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])
            assert reopened.has_pending() is True
            assert ledger.request_count <= envelope["requestsUpper"]["budget"]
        elif diagnostic_probe:
            report = execute()
            assert report["stageExecutionStatus"] == "COMPLETED"
            assert report["status"] == ("COMPLETED" if probe == "known_task_usage" else "UNKNOWN")
            assert guard.request_count == envelope["normalRequests"]["lease"]
            assert ledger.request_count == envelope["requestsUpper"]["budget"] - int(probe == "unknown_stage_usage")
            assert candidate.request_count == envelope["normalRequests"]["candidate"]
            assert checkpoints.request_count == envelope["normalRequests"]["checkpoint"]
            if probe == "unknown_stage_usage":
                assert report["budgetLifecycle"]["stageUsageUnknown"] == ["verify"]
        else:
            with pytest.raises(PipelineRunnerError, match="pipeline_lease_request_budget_exceeded"):
                execute()
            assert guard.request_count == 32
        # Still no activation, even when a later lease budget blocks completion.
        stats = requests.get(url + "/fixture/stats", headers={"Authorization": "Bearer fixture-only"}, timeout=5).json()
        assert stats["active"] == 0
        authority_requests = stats["http"].get("/api/v1/admin/pipeline-window/status", 0) + stats["http"].get("/api/v1/admin/pipeline-window/recovery", 0)
        assert authority_requests == guard.request_count + (2 if recovery else 1) * int(probe == "existing_hold")
        if probe in {"understated_control", "resume_scope", "understated_r2", "unknown_control_cost"}:
            assert stats["budgetReservations"] == {}
            assert stats["r2Put"] == 0
            return
        assert len(stats["budgetReservations"]) == 1
        held = stats["budgetReservations"][task]
        if probe == "known_task_usage":
            assert held["status"] == "SUCCEEDED"
            assert held["consumed"] == dict.fromkeys(RESOURCE_DIMENSIONS, 3)
            assert held["cost"] == upper
            assert held["settled"] == "settled"
            return
        if probe == "authority_lost":
            assert held["status"] == "RUNNING"  # Stale authority cannot settle/free it.
            assert held["consumed"] == dict.fromkeys(RESOURCE_DIMENSIONS, 0)
            assert all(held["authorized"][key] >= 1 for key in RESOURCE_DIMENSIONS)
            assert stats["r2Put"] == 0
            assert held["cost"] == upper
            return
        if probe == "existing_hold":
            assert held["status"] == "RESERVED"
            assert stats["http"]["/api/v1/admin/pipeline-window/budget"] == 3  # seed GET/POST + rejected task GET
            assert stats["r2Put"] == 0
            assert held["cost"]["r2Bytes"] == upper["r2Bytes"]
            return
        assert held["status"] in {"RUNNING", "UNKNOWN"}
        if probe == "unknown_stage_usage":
            assert held["status"] == "UNKNOWN"
            assert held["consumed"] == dict.fromkeys(RESOURCE_DIMENSIONS, 2)
            assert all(held["authorized"][key] >= held["consumed"][key] + 1 for key in RESOURCE_DIMENSIONS)
        if probe == "lost_candidate_reply":
            assert held["status"] == "UNKNOWN"
            assert stats["http"]["/api/v1/admin/read-model-import"] == 1
        assert held["cost"]["r2Bytes"] == upper["r2Bytes"]
        if probe in {"wrong_source_hash", "wrong_data_date"}:
            assert stats["r2Put"] == 0
            assert held["status"] == "UNKNOWN"
            return
        assert stats["r2Put"] > 0  # Real stage and candidate transports were reached.
    finally:
        process.terminate()
        process.wait(timeout=5)
        print(process.stderr.read()[-1500:])
