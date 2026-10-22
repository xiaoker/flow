from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest
import requests

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_preparation import CONTRACT, prepare_candidate_authority
from trading_desk.pipeline_entry import load_candidate_cost_policy, run_candidate_entry, candidate_preparation_cost_upper
from trading_desk.pipeline_checkpoint_preflight import FreshRecoveryCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.recovery_execution import build_recovery_plan, plan_bytes
from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS
from test_pipeline_entry import core_request, ROOT, LOCK, RUNTIME_LOCK, sha

NOW = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
STAMP = "2026-10-03T02:00:00Z"
EXPIRY = "2026-10-03T02:10:00Z"
ENV = {"FLOW_PUBLIC_URL": "http://127.0.0.1:1", "FLOW_INGEST_API_KEY": "fixture-only",
    "FLOW_CF_ACCESS_CLIENT_ID": "fixture-id", "FLOW_CF_ACCESS_CLIENT_SECRET": "fixture-secret"}


def inputs(tmp_path, previous=None):
    request, source = core_request(tmp_path)
    request["lease"].update(attemptCount=2, expiresAt=EXPIRY)
    profile = FreshRecoveryCandidateRequestProfile("a" * 64, 7, recovery_id="preparation-fixture", plan_sha256="b" * 64)
    policy = load_candidate_cost_policy(ROOT, profile=profile)
    extra = candidate_preparation_cost_upper(policy)
    plan = build_recovery_plan(campaign_id="preparation-fixture", recovery_id="preparation-fixture",
        run_key=request["identity"]["runKey"], data_dates=[request["identity"]["targetDate"]],
        required_stages=["dashboard", "publish"], artifact_root=".recovery/preparation-fixture",
        state_dir=".recovery/preparation-fixture/state", max_dates=1, max_requests=1000,
        deadline="2026-10-03T03:00:00Z", budget_manifest={"runnerBudgetContract": "flow-shared-budget-v1",
            "candidatePreparationContract": CONTRACT, "candidatePreparationUpperBound": extra.as_dict(),
            "taskCostUpperBound": policy["task"].add(extra).as_dict()})
    request.update(mode="recovery", recovery={"recoveryId": plan["recoveryId"], "planSha256": plan["planHash"]},
        authorization={"kind": "approved-recovery", "verified": True, "planSha256": plan["planHash"]})
    if previous is None:
        previous = {"runKey": request["identity"]["runKey"], "targetDate": request["identity"]["targetDate"],
            "status": "COMPLETED", "attemptCount": 1, "updatedAt": STAMP, "leaseOwner": None, "recoveryId": None}
    units = ("rows", "rows", "requests", "minutes", "bytes", "operations", "operations")
    evidence = {}
    measured = "2026-10-03T02:00:00.000Z"
    for d, unit in zip(RESOURCE_DIMENSIONS, units):
        if d == "r2Bytes":
            period = {"kind": "instant", "start": measured, "end": measured}
        elif d == "actionsMinutes":
            period = {"kind": "calendar_month", "start": "2026-10-01T00:00:00.000Z", "end": "2026-11-01T00:00:00.000Z"}
        elif d in {"r2ClassA", "r2ClassB"}:
            period = {"kind": "billing_cycle", "start": "2026-09-16T00:00:00.000Z", "end": "2026-10-16T00:00:00.000Z"}
        else:
            period = {"kind": "utc_day", "start": "2026-10-03T00:00:00.000Z", "end": "2026-10-04T00:00:00.000Z"}
        evidence[d] = {"value": 0, "unit": unit, "basis": "measured", "scope": "account", "measuredAt": measured,
            "precision": "exact", "period": period}
    package = {"contract": CONTRACT, "producerBuildSha": sha(), "requestSha256": canonical_sha256(request),
        "snapshotSha256": request["stageConfigs"]["build"]["snapshotSha256"], "planSha256": plan["planHash"],
        "scopeSha256": plan_bytes(plan)[1], "expiresAt": EXPIRY,
        "accountReconciliation": {"operation": "account_reconcile", "operationId": "a" * 64,
            "expectedRevision": None, "dryRun": False, "evidenceContract": "flow-account-evidence-v2",
            "evidenceReference": "official-usage-sha256:" + "d" * 64, "evidence": evidence},
        "registration": {"expectedAttemptCount": 1, "expectedUpdatedAt": STAMP,
            "previousStateJson": json.dumps(previous), "reasonCode": "controlled_canary",
            "approvalReference": "fixture-approval", "authorizedAt": STAMP, "expiresAt": "2026-10-03T03:00:00Z"}}
    return package, request, source, plan


def test_checkpoint_capacity_failure_happens_before_any_authority_call(tmp_path, monkeypatch):
    from trading_desk.pipeline_checkpoint_bundle import CheckpointBundleError
    package, request, source, plan = inputs(tmp_path)
    transport = Sequence(package, request)
    def oversized(**kwargs):
        raise CheckpointBundleError("bundle_transport_size_exceeded")
    monkeypatch.setattr("trading_desk.pipeline_checkpoint_bundle.build_stage_bundle", oversized)
    with pytest.raises(PipelineRunnerError, match="preparation_checkpoint_capacity_invalid"):
        execute(tmp_path, package, request, source, plan, session=transport)
    assert transport.calls == []


def test_publication_payload_limit_is_checked_before_authority(tmp_path):
    import hashlib
    package, request, source, plan = inputs(tmp_path)
    snapshot = json.loads(source.read_bytes())
    snapshot["capacityRegressionPadding"] = "x" * 1500001
    source.write_text(json.dumps(snapshot))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    request["stageConfigs"]["build"]["snapshotSha256"] = digest
    package.update(snapshotSha256=digest, requestSha256=canonical_sha256(request))
    transport = Sequence(package, request)
    with pytest.raises(PipelineRunnerError, match="preparation_candidate_payload_invalid"):
        execute(tmp_path, package, request, source, plan, session=transport)
    assert transport.calls == []


class Reply:
    def __init__(self, code, body):
        self.status_code, self.body = code, body
    def iter_content(self, size):
        yield json.dumps(self.body).encode()
    def close(self):
        pass


class Sequence:
    adapters = {}
    def __init__(self, package, request, *, fault=None):
        self.calls, self.package, self.payload, self.fault = [], package, request, fault
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        _require_index = len(self.calls)
        p = self.package
        owner = self.payload["lease"]["owner"]
        identity = self.payload["identity"]
        common = {"runKey": identity["runKey"], "recoveryId": "preparation-fixture"}
        claim = {**common, "decision": "acquired", "status": "DISPATCHED", "leaseOwner": owner,
            "attemptCount": 2, "leaseExpiresAt": "2026-10-03T02:20:00Z"}
        replies = [
            (200, json.loads(p["registration"]["previousStateJson"])),
            (404, {"error": "recovery_not_found"}),
            (200, {"decision": "account_reconciliation_prepared", "expectedRevision": None, "executionAllowed": False}),
            (200, {"decision": "account_reconciled", "scope": "flow-account-v1", "executionAllowed": False,
                "replayed": False, "evidenceReference": p["accountReconciliation"]["evidenceReference"],
                "evidenceSha256": canonical_sha256(p["accountReconciliation"]["evidence"])}),
            (201, {**common, "decision": "registered", "status": "AUTHORIZED", "planStorageDecision": "stored"}),
            (200, {**common, **p["registration"], "status": "AUTHORIZED", "planSha256": p["planSha256"], "scopeSha256": p["scopeSha256"]}),
            (200, claim),
            (200, {**claim, "windowDate": identity["windowDate"], "targetDate": identity["targetDate"],
                "sourceFingerprint": p["snapshotSha256"]}),
        ]
        if self.fault == "claim_timeout" and _require_index == 7:
            raise requests.Timeout("fixture-secret should never be echoed")
        code, body = replies[_require_index - 1]
        if _require_index in {3, 4}:
            body.update(scope="flow-account-v1", epochDay="2026-10-03", epochMonth="2026-10",
                evidenceContract=p["accountReconciliation"]["evidenceContract"],
                evidenceReference=p["accountReconciliation"]["evidenceReference"],
                evidenceSha256=canonical_sha256(p["accountReconciliation"]["evidence"]))
            if _require_index == 3:
                body["remoteWrites"] = False
            else:
                body["revision"] = 0
        if self.fault == "receipt" and _require_index == 4:
            body["revision"] = 99
        if self.fault == "disabled" and _require_index == 2:
            body = {"error": "pipeline_recovery_disabled"}
        if self.fault == "changed" and _require_index == 1:
            body["attemptCount"] = 3
        if self.fault == "cas" and _require_index == 3:
            code, body = 409, {"error": "budget_revision_conflict"}
        if self.fault == "leased" and _require_index == 7:
            body = {**body, "decision": "skip_leased"}
        if self.fault == "wrong_date" and _require_index == 8:
            body["targetDate"] = "2026-08-01"
        return Reply(code, body)


def execute(tmp_path, package, request, source, plan, **kwargs):
    return prepare_candidate_authority(package, canonical_sha256(package), request, snapshot_path=source,
        approved_plan=plan, producer_build_sha=sha(), project_root=ROOT, output_path=tmp_path / "preparation.json",
        environment=ENV, now=NOW, allow_loopback_fixture=True, **kwargs)


@pytest.mark.parametrize("fault,count", [(None, 8), ("changed", 1), ("disabled", 2), ("cas", 3),
    ("claim_timeout", 7), ("leased", 7), ("wrong_date", 8), ("receipt", 4)])
def test_one_shot_sequence_and_ambiguous_stop(tmp_path, fault, count):
    package, request, source, plan = inputs(tmp_path)
    transport = Sequence(package, request, fault=fault)
    if fault is None:
        report = execute(tmp_path, package, request, source, plan, session=transport)
        assert report["status"] == "CANDIDATE_AUTHORITY_PREPARED"
    else:
        with pytest.raises(PipelineRunnerError):
            execute(tmp_path, package, request, source, plan, session=transport)
        report = json.loads((tmp_path / "preparation.json").read_bytes())
        assert report["status"] == ("BLOCKED" if count <= 2 else "UNKNOWN")
    assert len(transport.calls) == count
    assert all(call[2]["allow_redirects"] is False for call in transport.calls)
    assert report["candidateExecuted"] is report["activeReleaseChanged"] is report["sourceCollected"] is False
    assert report["actualProviderUsageKnown"] is False
    assert report["combinedUpperBound"]["workerRequests"]["value"] > 740
    assert "fixture-secret" not in (tmp_path / "preparation.json").read_text()
    with pytest.raises(FileExistsError):
        execute(tmp_path, package, request, source, plan, session=transport)
    assert len(transport.calls) == count  # An existing intent cannot be reset/replayed.
    if fault == "claim_timeout":
        assert report["steps"][-1]["status"] == "PENDING"
        assert report["steps"][-1]["operationId"] == plan["planHash"]


@pytest.mark.parametrize("fault", ["confirmation", "source", "expiry", "generation", "unknown_budget", "over_budget", "secret"])
def test_invalid_package_has_no_http_or_state(tmp_path, fault):
    package, request, source, plan = inputs(tmp_path)
    if fault == "source": package["snapshotSha256"] = "0" * 64
    if fault == "expiry": package["expiresAt"] = STAMP
    if fault == "generation": package["registration"]["expectedAttemptCount"] = 9
    if fault == "unknown_budget": package["accountReconciliation"]["evidence"]["r2Bytes"]["value"] = None
    if fault == "over_budget":
        config = json.loads((ROOT / "config/resource-budget.json").read_bytes())
        package["accountReconciliation"]["evidence"]["actionsMinutes"]["value"] = config["limits"]["githubActionsMinutesPerMonth"]
    if fault == "secret": package["token"] = "fixture-secret-only"
    transport = Sequence(package, request)
    with pytest.raises(PipelineRunnerError):
        prepare_candidate_authority(package, "0" * 64 if fault == "confirmation" else canonical_sha256(package), request,
            snapshot_path=source, approved_plan=plan, producer_build_sha=sha(), project_root=ROOT,
            output_path=tmp_path / "preparation.json", environment=ENV, now=NOW, session=transport, allow_loopback_fixture=True)
    assert transport.calls == [] and not (tmp_path / "preparation.json").exists()


def test_existing_manual_workflow_is_optional_and_keeps_preparation_evidence():
    text = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    assert "schedule:" not in text and "pull_request:" not in text
    assert "inputs.execution_mode == 'candidate' && inputs.preparation_json != ''" in text
    assert text.index("Prepare one exact archived source") < text.index("Prepare explicitly approved authority") < text.index("Run the shared candidate executor")
    assert "${{ inputs.preparation_json }}" in text and "${{ secrets.FLOW_INGEST_API_KEY }}" in text
    assert "${{ runner.temp }}/flow-pipeline-candidate/preparation.json" in text
    assert "timeout-minutes: 10" in text and "PIPELINE_RECOVERY_ENABLED" not in text
    assert "if: ${{ inputs.preparation_json == '' }}" in text and "--run-candidate" in text


def test_preparation_cost_stays_in_same_hold_across_owner_attempts(tmp_path):
    package, request, source, plan = inputs(tmp_path)
    args = {"recovery_id": plan["recoveryId"], "plan_sha256": plan["planHash"]}
    fresh = FreshRecoveryCandidateRequestProfile("a" * 64, 7, **args)
    resume = ResumeRecoveryCandidateRequestProfile("a" * 64, 7, **args)
    legacy = load_candidate_cost_policy(ROOT, profile=fresh)
    first = load_candidate_cost_policy(ROOT, profile=fresh, approved_recovery_plan=plan)
    second = load_candidate_cost_policy(ROOT, profile=resume, approved_recovery_plan=plan)
    assert first["task"].numeric() == second["task"].numeric()
    assert first["task"].numeric() == legacy["task"].add(candidate_preparation_cost_upper(legacy)).numeric()
    assert legacy["task"].metrics["workerRequests"].value == 740
    bad = json.loads(json.dumps(plan))
    bad["budgetManifest"]["candidatePreparationUpperBound"]["workerRequests"]["value"] = 999
    with pytest.raises(PipelineRunnerError, match="entry_preparation_budget_invalid"):
        load_candidate_cost_policy(ROOT, profile=fresh, approved_recovery_plan=bad)


@pytest.mark.parametrize("entry_mode,large_source", [("functions", False), ("cli", False), ("functions", True)])
def test_real_worker_preparation_then_real_entry_keeps_unknown_hold(tmp_path, monkeypatch, capsys, entry_mode, large_source):
    package, request, source, plan = inputs(tmp_path)
    if large_source:
        import hashlib
        snapshot = json.loads(source.read_bytes())
        # Exceed checkpoint JSON/base64 capacity while staying below the
        # separate 1.5 MB per-publication-artifact contract.
        snapshot["capacityRegressionPadding"] = "official-input-" * (1048576 // len("official-input-"))
        source.write_text(json.dumps(snapshot))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        request["stageConfigs"]["build"]["snapshotSha256"] = digest
        package.update(snapshotSha256=digest, requestSha256=canonical_sha256(request))
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/full-pipeline-server.mjs")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ,
            "FLOW_FULL_FIXTURE_RUN_KEY": request["identity"]["runKey"], "FLOW_FULL_FIXTURE_TARGET": request["identity"]["targetDate"],
            "FLOW_FULL_FIXTURE_OWNER": request["lease"]["owner"], "FLOW_FULL_FIXTURE_PLAN": json.dumps(plan),
            "FLOW_FULL_FIXTURE_PREPARATION": "1", "FLOW_FULL_FIXTURE_REQUEST_LIMIT": "1000"})
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit()
        origin = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer fixture-only"}
        previous = requests.get(origin + "/api/v1/admin/pipeline-window/status", headers=headers,
            params={"runKey": request["identity"]["runKey"]}, timeout=5).json()
        package["registration"]["previousStateJson"] = json.dumps(previous)
        env = {**ENV, "FLOW_PUBLIC_URL": origin}
        if entry_mode == "cli":
            # Trusted local clock/origin seam only; CLI assembly and server remain real.
            import sys
            import trading_desk.pipeline_preparation as preparation
            import trading_desk.pipeline_entry as entry
            def local_prepare(*args, **kwargs):
                return prepare_candidate_authority(*args, **{**kwargs, "now": NOW, "allow_loopback_fixture": True})
            def local_entry(*args, **kwargs):
                return run_candidate_entry(*args, **{**kwargs, "now": NOW, "allow_loopback_fixture": True})
            monkeypatch.setattr(preparation, "prepare_candidate_authority", local_prepare)
            monkeypatch.setattr(entry, "run_candidate_entry", local_entry)
            for name, value in {**env, "FLOW_ENTRY_PREPARATION_JSON": json.dumps(package),
                    "FLOW_ENTRY_PREPARATION_SHA256": canonical_sha256(package)}.items():
                monkeypatch.setenv(name, value)
            (source.parent / "request.json").write_text(json.dumps(request))
            (source.parent / "approved-plan.json").write_text(json.dumps(plan))
            (source.parent / "dashboard-snapshot.json").write_bytes(source.read_bytes())
            monkeypatch.chdir(ROOT)
            monkeypatch.setattr(sys, "argv", ["pipeline-preparation", "--run-candidate", "--source-directory",
                str(source.parent), "--producer-build-sha", sha(), "--output", str(tmp_path / "preparation.json")])
            with pytest.raises(SystemExit) as stopped:
                preparation.main()
            assert stopped.value.code == 2
            assert json.loads(capsys.readouterr().out)["status"] == "UNKNOWN"
            report = json.loads((tmp_path / "preparation.json").read_bytes())
            assert report["candidateInvocationStarted"] is True and report["candidateOutcome"] == "UNKNOWN"
            stats = requests.get(origin + "/fixture/stats", headers=headers, timeout=5).json()
            assert stats["active"] == 0 and len(stats["budgetReservations"]) == 1
            assert next(iter(stats["budgetReservations"].values()))["cost"] == {
                d: report["combinedUpperBound"][d]["value"] for d in RESOURCE_DIMENSIONS}
            return
        report = prepare_candidate_authority(package, canonical_sha256(package), request, snapshot_path=source,
            approved_plan=plan, producer_build_sha=sha(), project_root=ROOT, output_path=tmp_path / "preparation.json",
            environment=env, now=NOW, allow_loopback_fixture=True)
        assert report["status"] == "CANDIDATE_AUTHORITY_PREPARED" and report["requests"] == 8
        result = run_candidate_entry(request, snapshot_path=source, producer_build_sha=sha(), dependency_lock_paths=[LOCK, RUNTIME_LOCK],
            project_root=ROOT, state_dir=tmp_path / "candidate", output_path=tmp_path / "result.json",
            budget_journal=tmp_path / "budget.sqlite", cost_policy=None, environment=env,
            approved_recovery_plan=plan, now=NOW, allow_loopback_fixture=True)
        assert result["stageExecutionStatus"] == "COMPLETED" and result["status"] == "UNKNOWN"
        if large_source:
            bundles = list((tmp_path / "candidate" / "bundles").glob("*.json"))
            assert bundles and any(json.loads(p.read_bytes())["schemaVersion"] == 2 for p in bundles)
            assert all(p.stat().st_size <= 1048576 for p in bundles)
        stats = requests.get(origin + "/fixture/stats", headers=headers, timeout=5).json()
        assert stats["active"] == 0 and len(stats["budgetReservations"]) == 1
        assert next(iter(stats["budgetReservations"].values()))["status"] == "UNKNOWN"
        assert next(iter(stats["budgetReservations"].values()))["cost"] == {
            d: report["combinedUpperBound"][d]["value"] for d in RESOURCE_DIMENSIONS}
    except PipelineRunnerError as error:
        process.terminate()
        details = process.communicate(timeout=5)[1]
        pytest.fail(f"actual Worker rejected preparation: {error.code}\n{details}")
    finally:
        process.terminate()
        process.wait(timeout=5)
