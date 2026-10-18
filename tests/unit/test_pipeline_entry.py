from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from trading_desk.pipeline_entry import (read_entry_request, dry_run_entry, MAX_REQUEST_BYTES,
    prepare_core_candidate, candidate_credentials)
from trading_desk.pipeline_runner import PipelineRunnerError, dependency_lock_digest

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "web/package-lock.json"
RUNTIME_LOCK = ROOT / "config/pipeline-python-runtime.lock"
FIXTURE = ROOT / "tests/fixtures/pipeline/v060-c-ordinary.json"


def sha():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def payload():
    result = read_entry_request(FIXTURE)
    result["dependencyLockSha256"] = dependency_lock_digest([LOCK, RUNTIME_LOCK], root=ROOT)["sha256"]
    return result


def test_dry_run_checks_real_checkout_and_plan_without_execution(monkeypatch):
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not make any network request")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    report = dry_run_entry(payload(), producer_build_sha=sha(),
        dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)
    assert report["status"] == "DECLARED_INPUT_CHECKED"
    assert report["liveExecutionAllowed"] is report["serverAuthorityVerified"] is False
    assert all(value is False for value in report["sideEffects"].values())


@pytest.mark.parametrize("bad_sha", ["main", "0" * 40])
def test_dry_run_rejects_wrong_checkout(bad_sha):
    with pytest.raises(PipelineRunnerError, match="entry_checkout_sha_"):
        dry_run_entry(payload(), producer_build_sha=bad_sha, dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)


def test_dry_run_reuses_lock_contract_and_rejects_mismatch():
    request = payload()
    request["dependencyLockSha256"] = "0" * 64
    with pytest.raises(PipelineRunnerError, match="dependency_lock_sha_mismatch"):
        dry_run_entry(request, producer_build_sha=sha(), dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)


def test_checkout_validation_ignores_git_repository_overrides(monkeypatch, tmp_path):
    from trading_desk.pipeline_entry import verify_checkout_sha, verify_clean_candidate_code
    root = tmp_path / "checkout"
    root.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for args in (["init"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                             "commit", "--allow-empty", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env,
                              check=True, capture_output=True, text=True).stdout.strip()
    (root / "config").mkdir()
    (root / "config/unreviewed.json").write_text("{}")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "untrusted-index"))
    verify_checkout_sha(root, expected)
    # It must inspect the checkout's uncommitted code, not the override.
    with pytest.raises(PipelineRunnerError, match="entry_candidate_code_dirty"):
        verify_clean_candidate_code(root)
    assert os.environ["GIT_DIR"] == str(tmp_path / "not-a-repository")


def test_request_read_error_is_sanitized(monkeypatch):
    def unreadable(*args, **kwargs):
        raise PermissionError("sensitive-path-or-credential")
    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(PipelineRunnerError, match="entry_request_unavailable") as rejected:
        read_entry_request(FIXTURE)
    assert "sensitive-path-or-credential" not in str(rejected.value)


def test_daily_limits_cannot_substitute_candidate_cost_policy(tmp_path):
    from trading_desk.pipeline_entry import load_candidate_cost_policy
    (tmp_path / "config").mkdir()
    (tmp_path / "config/resource-budget.json").write_text(json.dumps({
        "limits": {"d1RowsReadPerDay": 5000000}, "resourceAccounting": {}}))
    with pytest.raises(PipelineRunnerError, match="entry_cost_policy_not_established"):
        load_candidate_cost_policy(tmp_path)
    assert not (tmp_path / "state").exists()


def test_fixed_policy_derives_full_task_and_two_attempt_cost():
    from trading_desk.pipeline_entry import load_candidate_cost_policy
    from trading_desk.pipeline_checkpoint_preflight import FreshCandidateRequestProfile, ResumeCandidateRequestProfile
    first = FreshCandidateRequestProfile("a" * 64, 7)
    resume = ResumeCandidateRequestProfile("a" * 64, 7)
    cost = load_candidate_cost_policy(ROOT, profile=first)
    second = load_candidate_cost_policy(ROOT, profile=resume)
    assert cost["task"].all_known()
    assert cost["task"].metrics["workerRequests"].value == resume.envelope()["taskWorkerRequestsUpper"]
    assert cost["runtime"].metrics["actionsMinutes"].value == 10
    assert second["runtime"].metrics["actionsMinutes"].value == 20
    assert second["task"].metrics["workerRequests"].value == resume.envelope()["taskWorkerRequestsUpper"]
    assert second["task"] == cost["task"]


@pytest.mark.parametrize("fault", [None, "traversal", "duplicate", "wrong_date", "bad_redirect", "wrong_run", "secret",
    "expired", "oversized_metadata", "metadata_http", "metadata_json", "archive_http", "bad_zip", "zip_symlink", "zip_count",
    "retained_report", "retained_expired", "retained_wrong_run", "retained_wrong_hash", "publisher_report", "lookalike_report"])
def test_workflow_source_is_exact_bounded_and_never_forwards_token(tmp_path, monkeypatch, fault):
    import io
    import zipfile
    import hashlib
    from trading_desk.pipeline_entry import prepare_workflow_inputs
    import trading_desk.http as http
    source = json.dumps({"asOf": "2026-08-06" if fault != "wrong_date" else "2026-08-05"}).encode()
    request = {"identity": {"targetDate": "2026-08-06"},
               "stageConfigs": {"build": {"snapshotSha256": hashlib.sha256(source).hexdigest()}}}
    if fault == "secret":
        request["token"] = "never-echo-me"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dashboard-snapshot.json", source)
        if fault == "traversal":
            archive.writestr("../escape.json", "{}")
        if fault == "duplicate":
            archive.writestr("nested/source-bundle/dashboard-snapshot.json", source)
        if fault == "zip_symlink":
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "target")
        if fault == "zip_count":
            for i in range(64):
                archive.writestr(f"unused-{i}.json", "{}")
    data = buffer.getvalue()
    metadata = {"id": 17, "expired": False, "name": "market-data-collection-source-9",
                "workflow_run": {"id": 10 if fault == "wrong_run" else 9}, "size_in_bytes": len(data)}
    if fault and fault.startswith("retained_"):
        metadata["name"] = "market-data-9"
    if fault == "retained_expired":
        metadata["expired"] = True
    if fault == "retained_wrong_run":
        metadata["workflow_run"]["id"] = 10
    if fault == "retained_wrong_hash":
        request["stageConfigs"]["build"]["snapshotSha256"] = "0" * 64
    if fault == "publisher_report":
        metadata["name"] = "market-data-publish-9"
    if fault == "lookalike_report":
        metadata["name"] = "market-data-90"
    if fault == "expired":
        metadata["expired"] = True
    if fault == "oversized_metadata":
        metadata["size_in_bytes"] = 6 * 1024 * 1024 + 1
    signed = "https://fixture.blob.core.windows.net/archive?sig=private-value"
    calls = []
    class Reply:
        def __init__(self, status, data, headers=None):
            self.status_code, self.data, self.headers = status, data, headers or {}
        def iter_content(self, chunk_size):
            yield self.data
        def close(self):
            pass
    class Session:
        def request(self, method, url, **kwargs):
            calls.append((url, kwargs))
            assert kwargs["allow_redirects"] is False
            if len(calls) == 1:
                return Reply(503 if fault == "metadata_http" else 200,
                    b"invalid-json" if fault == "metadata_json" else json.dumps(metadata).encode())
            if len(calls) == 2:
                return Reply(302, b"", {"Location": "https://evil.invalid/" if fault == "bad_redirect" else signed})
            assert url == signed
            assert "Authorization" not in kwargs["headers"]
            return Reply(503 if fault == "archive_http" else 200, b"invalid-zip" if fault == "bad_zip" else data)
        def close(self):
            pass
    monkeypatch.setattr(http, "build_session", lambda settings: Session())
    env = {"GITHUB_TOKEN": "fixture-github-secret", "FLOW_ENTRY_REQUEST_JSON": json.dumps(request)}
    destination = tmp_path / "prepared"
    if fault not in {None, "retained_report"}:
        with pytest.raises(PipelineRunnerError) as rejected:
            prepare_workflow_inputs(destination, artifact_id="17", source_run_id="9", environment=env)
        assert "fixture-github-secret" not in str(rejected.value)
        assert "private-value" not in str(rejected.value)
        assert not destination.exists()
        if fault == "secret":
            assert not calls
    else:
        report = prepare_workflow_inputs(destination, artifact_id="17", source_run_id="9", environment=env)
        assert report["serverAuthorityVerified"] is False
        assert len(calls) == 3
        assert (destination / "dashboard-snapshot.json").read_bytes() == source
        assert (destination / "request.json").stat().st_mode & 0o777 == 0o600


def test_candidate_cli_requires_explicit_source_and_retained_journal(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload()))
    result = subprocess.run([sys.executable, "-m", "trading_desk.cli", "pipeline",
        "--request", str(request), "--execution-mode", "candidate", "--state-dir", str(tmp_path / "state"),
        "--output", str(tmp_path / "result.json"), "--dependency-lock", str(LOCK),
        "--dependency-lock", str(RUNTIME_LOCK), "--producer-build-sha", sha()], cwd=ROOT, env={**os.environ, "PYTHONPATH": "src:."},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert json.loads(result.stdout)["errorCode"] == "entry_candidate_paths_required"
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "result.json").exists()


def test_candidate_runtime_requires_both_locks_and_actual_versions(monkeypatch):
    from trading_desk.pipeline_entry import verify_candidate_runtime
    import importlib.metadata
    verify_candidate_runtime(ROOT, [LOCK, RUNTIME_LOCK])
    with pytest.raises(PipelineRunnerError, match="entry_runtime_lock_set_required"):
        verify_candidate_runtime(ROOT, [LOCK])
    original = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.0" if name == "requests" else original(name))
    with pytest.raises(PipelineRunnerError, match="entry_python_runtime_mismatch"):
        verify_candidate_runtime(ROOT, [LOCK, RUNTIME_LOCK])


@pytest.mark.parametrize("fault", ["malformed", "incomplete", "missing_package"])
def test_candidate_runtime_rejects_broken_lock_or_missing_package(tmp_path, monkeypatch, fault):
    from trading_desk.pipeline_entry import verify_candidate_runtime
    import importlib.metadata
    (tmp_path / "web").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "web/package-lock.json").write_bytes(LOCK.read_bytes())
    text = RUNTIME_LOCK.read_text()
    if fault == "malformed":
        text = "--index-url https://untrusted.invalid\n" + text
    if fault == "incomplete":
        text = text.replace("urllib3==2.7.0\n", "")
    runtime = tmp_path / "config/pipeline-python-runtime.lock"
    runtime.write_text(text)
    if fault == "missing_package":
        def missing(name):
            raise importlib.metadata.PackageNotFoundError(name)
        monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(PipelineRunnerError, match="entry_python_"):
        verify_candidate_runtime(tmp_path, [tmp_path / "web/package-lock.json", runtime])


@pytest.mark.parametrize("fault", ["schema", "evidence", "vector", "unknown"])
def test_fixed_cost_policy_rejects_malformed_or_unknown_bounds(tmp_path, fault):
    from trading_desk.pipeline_entry import load_candidate_cost_policy
    from trading_desk.pipeline_checkpoint_preflight import FreshCandidateRequestProfile
    config = json.loads((ROOT / "config/resource-budget.json").read_bytes())
    policy = config["resourceAccounting"]["candidateEntryPolicy"]
    if fault == "schema":
        policy["schemaVersion"] = 2
    elif fault == "evidence":
        policy["evidence"]["basis"] = "caller-numbers"
    elif fault == "vector":
        policy["control"]["lease"].pop("d1RowsRead")
    else:
        policy["control"]["lease"]["d1RowsRead"] = None
    (tmp_path / "config").mkdir()
    (tmp_path / "config/resource-budget.json").write_text(json.dumps(config))
    with pytest.raises(PipelineRunnerError, match="entry_cost_"):
        load_candidate_cost_policy(tmp_path, profile=FreshCandidateRequestProfile("a" * 64, 7))


def test_workflow_declared_dry_run_checks_supplied_request_without_network(tmp_path, monkeypatch):
    from trading_desk.pipeline_entry import prepare_declared_request
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("declared dry-run cannot perform network I/O")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    proposed = payload()
    proposed["dependencyLockSha256"] = "0" * 64
    destination = tmp_path / "declared"
    prepare_declared_request(destination, environment={"FLOW_ENTRY_REQUEST_JSON": json.dumps(proposed)})
    actual = read_entry_request(destination / "request.json")
    with pytest.raises(PipelineRunnerError, match="dependency_lock_sha_mismatch"):
        dry_run_entry(actual, producer_build_sha=sha(), dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)
    assert not (destination / "state").exists()


@pytest.mark.parametrize("key", ["token", "api_key", "CF-Access-Client-Secret", "password", "access_token", "GITHUB_TOKEN", "private-key"])
def test_credentials_rejected_recursively_without_echo(tmp_path, key):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"nested": [{key: "fixture-secret-only"}]}))
    with pytest.raises(PipelineRunnerError, match="inline_credential_forbidden") as rejected:
        read_entry_request(path)
    assert "fixture-secret-only" not in str(rejected.value)


def test_request_is_bounded_and_cannot_follow_symlink(tmp_path):
    path = tmp_path / "request.json"
    path.write_bytes(b" " * (MAX_REQUEST_BYTES + 1))
    with pytest.raises(PipelineRunnerError, match="entry_request_size_exceeded"):
        read_entry_request(path)
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(PipelineRunnerError, match="entry_request_path_invalid"):
        read_entry_request(linked)


def test_cli_dry_run_has_no_checkpoint_or_report_writes(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload()))
    result = subprocess.run([sys.executable, "-m", "trading_desk.cli", "pipeline",
        "--request", str(request), "--execution-mode", "dry-run", "--state-dir", str(tmp_path / "state"),
        "--output", str(tmp_path / "result.json"), "--dependency-lock", str(LOCK),
        "--dependency-lock", str(RUNTIME_LOCK), "--producer-build-sha", sha()], cwd=ROOT, env={**os.environ, "PYTHONPATH": "src:."},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["executionMode"] == "dry-run"
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "result.json").exists()


def core_request(tmp_path):
    import hashlib
    from test_pipeline_verify_adapter import setup
    setup(tmp_path)
    request = payload()
    request.pop("stageResults")
    request.pop("stageInputs", None)
    request["stages"] = ["build", "verify", "publish"]
    source = tmp_path / "source.json"
    request["identity"]["targetDate"] = json.loads(source.read_bytes())["asOf"]
    request["stageConfigs"] = {"build": {"snapshotSha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
    return request, source


def test_core_graph_derived_by_existing_adapters_is_stable_and_no_writes(tmp_path):
    request, source = core_request(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    first = prepare_core_candidate(request, snapshot_path=source, producer_build_sha=sha(),
        dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)
    second = prepare_core_candidate(request, snapshot_path=source, producer_build_sha=sha(),
        dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)
    assert first == second
    assert len(first[2]["artifacts"]) == 7
    assert first[0]["stageConfigs"]["verify"]["buildInputSha256"] == first[0]["stageInputs"]["build"]
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("fault", ["source_hash", "fixture_result", "derived_config"])
def test_candidate_preparation_rejects_untrusted_graph(tmp_path, fault):
    request, source = core_request(tmp_path)
    if fault == "source_hash":
        request["stageConfigs"]["build"]["snapshotSha256"] = "0" * 64
    elif fault == "fixture_result":
        request["stageResults"] = {}
    else:
        request["stageConfigs"]["verify"] = {"buildOutputSha256": "0" * 64}
    with pytest.raises(PipelineRunnerError, match="entry_"):
        prepare_core_candidate(request, snapshot_path=source, producer_build_sha=sha(),
            dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)


def test_environment_credentials_fail_without_echo_or_inline_fallback():
    with pytest.raises(PipelineRunnerError, match="entry_environment_credentials_required") as rejected:
        candidate_credentials({"FLOW_INGEST_API_KEY": "fixture-private-value"})
    assert "fixture-private-value" not in str(rejected.value)
    assert "fixture-private-value" not in repr(rejected.value)


def test_source_cannot_export_inline_secret_even_with_matching_hash(tmp_path):
    import hashlib
    request, source = core_request(tmp_path)
    value = json.loads(source.read_bytes())
    value["debug"] = {"api_key": "fixture-secret-only"}
    source.write_text(json.dumps(value))
    request["stageConfigs"]["build"]["snapshotSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(PipelineRunnerError, match="^inline_credential_forbidden$"):
        prepare_core_candidate(request, snapshot_path=source, producer_build_sha=sha(),
            dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT)


@pytest.mark.parametrize("lease_generation", [1, 2, 7])
def test_unknown_entry_cost_fails_before_journal_or_network(tmp_path, monkeypatch, lease_generation):
    from trading_desk.pipeline_entry import run_candidate_entry
    from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("unknown cost must not make network requests")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    request, source = core_request(tmp_path)
    # A fresh candidate can acquire a later global lease generation. Unknown
    # cost must still fail closed, not be mistaken for an invalid runner retry.
    request["lease"]["attemptCount"] = lease_generation
    unit = BudgetVector.estimated(dict.fromkeys(RESOURCE_DIMENSIONS, 1))
    policy = {"task": BudgetVector.unknown(), "stage": dict.fromkeys(("build", "verify", "publish"), unit),
        "control": dict.fromkeys(("lease", "budget"), unit), "transport": dict.fromkeys(("candidate", "checkpoint"), unit),
        "runtime": unit}
    with pytest.raises(PipelineRunnerError, match="entry_cost_policy_unknown"):
        run_candidate_entry(request, snapshot_path=source, producer_build_sha=sha(), dependency_lock_paths=[LOCK, RUNTIME_LOCK],
            project_root=ROOT, state_dir=tmp_path / "job", output_path=tmp_path / "result.json",
            budget_journal=tmp_path / "budget.sqlite", cost_policy=policy, environment={"FLOW_PUBLIC_URL": "http://127.0.0.1:1"},
            allow_loopback_fixture=True)
    assert not (tmp_path / "budget.sqlite").exists() and not (tmp_path / "job").exists()


@pytest.mark.parametrize("probe", ["fresh", "cli", "resume", "resume_compressed"])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("lease_generation", [1, 7])
def test_fixed_entry_assembly_calls_real_worker_and_keeps_unknown_usage(tmp_path, recovery, probe, monkeypatch, capsys, lease_generation):
    from datetime import datetime, timezone
    from trading_desk.pipeline_entry import run_candidate_entry, prepare_core_candidate, load_candidate_cost_policy
    from trading_desk.resource_budget import BudgetVector, RESOURCE_DIMENSIONS
    from trading_desk.recovery_execution import build_recovery_plan
    from trading_desk.pipeline_checkpoint_preflight import FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile
    from trading_desk.pipeline_checkpoint_preflight import ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile
    from trading_desk.pipeline_checkpoint_transport import MAX_OBJECT_BYTES
    import requests
    now = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
    request, source = core_request(tmp_path)
    if probe == "resume_compressed":
        import hashlib
        snapshot = json.loads(source.read_bytes())
        snapshot["capacityRegressionPadding"] = "official-input-" * (1048576 // len("official-input-"))
        source.write_text(json.dumps(snapshot))
        request["stageConfigs"]["build"]["snapshotSha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    request["lease"]["attemptCount"] = lease_generation
    upper = {**dict.fromkeys(RESOURCE_DIMENSIONS, 100000), "r2Bytes": 500000000}
    plan = None
    if recovery:
        plan = build_recovery_plan(campaign_id="entry-fixture", recovery_id="entry-fixture",
            run_key=request["identity"]["runKey"], data_dates=[request["identity"]["targetDate"]],
            required_stages=["dashboard", "publish"], artifact_root=".recovery/entry",
            state_dir=".recovery/entry/state", max_dates=1, max_requests=1000,
            deadline="2026-10-03T03:00:00Z", budget_manifest={"runnerBudgetContract": "flow-shared-budget-v1",
                "taskCostUpperBound": BudgetVector.estimated(upper).as_dict()})
        request.update(mode="recovery", recovery={"recoveryId": plan["recoveryId"], "planSha256": plan["planHash"]},
            authorization={"kind": "approved-recovery", "verified": True, "planSha256": plan["planHash"]})
    resolved, context, manifest = prepare_core_candidate(request, snapshot_path=source,
        producer_build_sha=sha(), dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT, now=now)
    profile = (FreshRecoveryCandidateRequestProfile("a" * 64, len(manifest["artifacts"]),
        recovery_id=plan["recoveryId"], plan_sha256=plan["planHash"]) if recovery else
        FreshCandidateRequestProfile("a" * 64, len(manifest["artifacts"])))
    total_profile = (ResumeRecoveryCandidateRequestProfile("a" * 64, len(manifest["artifacts"]),
        recovery_id=plan["recoveryId"], plan_sha256=plan["planHash"]) if recovery else
        ResumeCandidateRequestProfile("a" * 64, len(manifest["artifacts"])))
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/full-pipeline-server.mjs")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ,
            "FLOW_FULL_FIXTURE_RUN_KEY": context.identity.run_key, "FLOW_FULL_FIXTURE_TARGET": context.identity.target_date,
            "FLOW_FULL_FIXTURE_OWNER": context.lease_owner,
            "FLOW_FULL_FIXTURE_GENERATION": str(lease_generation),
            "FLOW_FULL_FIXTURE_QUERY_PLANS": "1",
            "FLOW_FULL_FIXTURE_REQUEST_LIMIT": str(total_profile.envelope()["taskWorkerRequestsUpper"] + 16),
            **({"FLOW_FULL_FIXTURE_PLAN": json.dumps(plan)} if plan else {})})
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), "local built-Worker fixture failed to start"
        origin = f"http://127.0.0.1:{port}"
        policy = load_candidate_cost_policy(ROOT, profile=profile)
        options = dict(snapshot_path=source, producer_build_sha=sha(),
            dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT, state_dir=tmp_path / "run", output_path=tmp_path / "result.json",
            budget_journal=tmp_path / "budget.sqlite", cost_policy=None, now=now, approved_recovery_plan=plan,
            environment={"FLOW_PUBLIC_URL": origin, "FLOW_INGEST_API_KEY": "fixture-only",
                "FLOW_CF_ACCESS_CLIENT_ID": "fixture-id", "FLOW_CF_ACCESS_CLIENT_SECRET": "fixture-secret"},
            allow_loopback_fixture=True)
        if probe == "cli":
            # Only the trusted local-origin/clock seam is overridden. The CLI
            # parser, assembly, transports, Worker and SQL remain real.
            import trading_desk.pipeline_entry as entry
            import trading_desk.cli as cli
            def local_entry(payload, **kwargs):
                return run_candidate_entry(payload, **{**kwargs, "now": now, "allow_loopback_fixture": True})
            monkeypatch.setattr(entry, "run_candidate_entry", local_entry)
            monkeypatch.setattr(entry, "verify_clean_candidate_code", lambda root: None)
            for name, value in options["environment"].items():
                monkeypatch.setenv(name, value)
            request_path = tmp_path / "request.json"
            request_path.write_text(json.dumps(request))
            argv = ["trading-desk", "pipeline", "--execution-mode", "candidate", "--request", str(request_path),
                "--snapshot", str(source), "--state-dir", str(options["state_dir"]), "--output", str(options["output_path"]),
                "--budget-journal", str(options["budget_journal"]), "--dependency-lock", str(LOCK),
                "--dependency-lock", str(RUNTIME_LOCK), "--producer-build-sha", sha()]
            if plan:
                plan_path = tmp_path / "plan.json"
                plan_path.write_text(json.dumps(plan))
                argv += ["--approved-recovery-plan", str(plan_path)]
            monkeypatch.setattr(sys, "argv", argv)
            with pytest.raises(SystemExit) as rejected:
                cli.main()
            assert rejected.value.code == 2  # UNKNOWN must not look successful.
            assert json.loads(capsys.readouterr().out)["status"] == "UNKNOWN"
            report = json.loads(options["output_path"].read_bytes())
        elif probe in {"resume", "resume_compressed"}:
            from trading_desk.pipeline_publish_adapter import CoreCandidatePublishAdapter
            real_run = CoreCandidatePublishAdapter.run
            def interrupt(*args, **kwargs):
                raise SystemExit("fixture crash before candidate")
            monkeypatch.setattr(CoreCandidatePublishAdapter, "run", interrupt)
            with pytest.raises(SystemExit, match="fixture crash"):
                run_candidate_entry(request, **options)
            if probe == "resume_compressed":
                bundles = list((options["state_dir"] / "bundles").glob("*.json"))
                assert bundles and any(json.loads(p.read_bytes())["schemaVersion"] == 2 for p in bundles)
                assert all(p.stat().st_size <= MAX_OBJECT_BYTES for p in bundles)
            monkeypatch.setattr(CoreCandidatePublishAdapter, "run", real_run)
            headers = {"Authorization": "Bearer fixture-only"}
            assert requests.post(origin + "/fixture/expire-lease", headers=headers, timeout=5).status_code == 200
            next_request = json.loads(json.dumps(request))
            next_request["lease"].update(owner="second-owner", callbackOwner="second-owner", attemptCount=lease_generation + 1)
            claim = {"runKey": context.identity.run_key, "leaseOwner": "second-owner"}
            if recovery:
                claim["recoveryId"] = plan["recoveryId"]
            else:
                claim.update(windowDate=context.identity.window_date, windowName="reconcile", contractVersion="v1", targetDate=context.identity.target_date)
            path = "/api/v1/admin/pipeline-window/recovery/claim" if recovery else "/api/v1/admin/pipeline-window/claim"
            response = requests.post(origin + path, headers=headers, json=claim, timeout=5)
            assert response.status_code == 200, response.text
            handoff = json.loads((options["state_dir"] / "handoff.json").read_bytes())["sha256"]
            config = {"request": next_request, "plan": plan, "source": str(source), "origin": origin,
                      "root": str(tmp_path), "handoff": handoff, "producer": sha()}
            child = subprocess.run([sys.executable, "-c",
                "import json,sys; from test_pipeline_entry import entry_resume_child; entry_resume_child(json.loads(sys.argv[1]))", json.dumps(config)],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": "src:.:tests/unit"}, capture_output=True, text=True, timeout=30)
            assert child.returncode == 0, child.stderr
            report = json.loads(child.stdout)
            assert report["plan"]["records"][0]["reused"] is True
            assert report["plan"]["records"][1]["reused"] is True
        else:
            report = run_candidate_entry(request, **options)
        assert report["stageExecutionStatus"] == "COMPLETED"
        assert report["status"] == "UNKNOWN"
        assert report["budgetLifecycle"]["stageUsageUnknown"] == (["publish"] if probe in {"resume", "resume_compressed"} else ["build", "verify", "publish"])
        assert report["sideEffects"]["activeReleaseChanged"] is False
        stats = requests.get(origin + "/fixture/stats", headers={"Authorization": "Bearer fixture-only"}, timeout=5).json()
        assert stats["active"] == 0
        plans = [detail for query in stats["queryPlans"] for detail in query]
        assert plans and any("SEARCH" in detail for detail in plans)
        assert all(not any(f"SCAN {name}" in detail for name in
            ("pipeline_window", "pipeline_window_recovery", "pipeline_budget_account", "release_manifest", "p", "r"))
            for detail in plans)
        assert len(stats["budgetReservations"]) == 1
        hold = next(iter(stats["budgetReservations"].values()))
        assert hold["status"] == "UNKNOWN" and hold["cost"] == {d: policy["task"].metrics[d].value for d in RESOURCE_DIMENSIONS}
        assert hold["consumed"] == dict.fromkeys(RESOURCE_DIMENSIONS, 0)
        assert stats["http"]["/api/v1/admin/read-model-import"] == len(manifest["artifacts"])
        encoded_report = json.dumps(report)
        assert "fixture-secret" not in encoded_report and "fixture-only" not in encoded_report
    finally:
        process.terminate()
        process.wait(timeout=5)


def entry_resume_child(config):
    from datetime import datetime, timezone
    from trading_desk.pipeline_entry import run_candidate_entry
    root = Path(config["root"])
    report = run_candidate_entry(config["request"], snapshot_path=Path(config["source"]),
        producer_build_sha=config["producer"], dependency_lock_paths=[LOCK, RUNTIME_LOCK], project_root=ROOT,
        state_dir=root / "second", output_path=root / "second-result.json", budget_journal=root / "budget.sqlite",
        cost_policy=None, now=datetime(2026, 10, 3, 2, tzinfo=timezone.utc), approved_recovery_plan=config["plan"],
        resume_handoff_sha256=config["handoff"], allow_loopback_fixture=True,
        environment={"FLOW_PUBLIC_URL": config["origin"], "FLOW_INGEST_API_KEY": "fixture-only",
                     "FLOW_CF_ACCESS_CLIENT_ID": "fixture-id", "FLOW_CF_ACCESS_CLIENT_SECRET": "fixture-secret"})
    print(json.dumps(report))


def test_candidate_attempt_history_is_durable_bounded_and_not_a_lease(tmp_path):
    from trading_desk.resource_budget_store import RemoteBudgetOperationStore
    path = tmp_path / "attempt.sqlite"
    store = RemoteBudgetOperationStore.initialize(path, task_id="candidate-test", input_sha256="a" * 64)
    assert store.bind_candidate_attempt(owner="first", generation=7, resume=False) == 1
    reopened = RemoteBudgetOperationStore(path, task_id="candidate-test", input_sha256="a" * 64)
    for owner, generation, resume in [("first", 7, False), ("first", 8, True), ("second", 7, True)]:
        with pytest.raises(ValueError, match="candidate_attempt_"):
            reopened.bind_candidate_attempt(owner=owner, generation=generation, resume=resume)
    assert reopened.bind_candidate_attempt(owner="second", generation=9, resume=True) == 2
    assert reopened.candidate_attempt(owner="second", generation=9) == 2
    with pytest.raises(ValueError, match="candidate_attempt_identity_conflict"):
        store.candidate_attempt(owner="first", generation=7)
    with pytest.raises(ValueError, match="candidate_attempt_resume_unproved_or_exhausted"):
        reopened.bind_candidate_attempt(owner="third", generation=10, resume=True)
    assert reopened.pending() is None  # No remote permission/allowance invented.


def test_candidate_resume_cannot_invent_missing_history(tmp_path):
    from trading_desk.resource_budget_store import RemoteBudgetOperationStore
    store = RemoteBudgetOperationStore.initialize(tmp_path / "empty.sqlite", task_id="candidate-test", input_sha256="a" * 64)
    with pytest.raises(ValueError, match="candidate_attempt_resume_unproved_or_exhausted"):
        store.bind_candidate_attempt(owner="second", generation=8, resume=True)


@pytest.mark.parametrize("resume", [False, True])
def test_candidate_attempt_concurrent_connections_have_one_winner(tmp_path, resume):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from trading_desk.resource_budget_store import RemoteBudgetOperationStore
    path = tmp_path / "concurrent.sqlite"
    original = RemoteBudgetOperationStore.initialize(path, task_id="candidate-test", input_sha256="a" * 64)
    if resume:
        original.bind_candidate_attempt(owner="first", generation=7, resume=False)
    barrier = Barrier(2)
    def compete(owner):
        store = RemoteBudgetOperationStore(path, task_id="candidate-test", input_sha256="a" * 64)
        barrier.wait()
        try:
            return store.bind_candidate_attempt(owner=owner, generation=8 if resume else 7, resume=resume)
        except ValueError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ["owner-a", "owner-b"]))
    assert results.count(2 if resume else 1) == results.count("rejected") == 1


def test_candidate_attempt_binding_retains_unconfirmed_operation(tmp_path):
    from trading_desk.resource_budget_store import RemoteBudgetOperationStore
    path = tmp_path / "pending.sqlite"
    store = RemoteBudgetOperationStore.initialize(path, task_id="candidate-test", input_sha256="a" * 64)
    store.bind_candidate_attempt(owner="first", generation=7, resume=False)
    operation = {"taskContract": "flow-budget-task-v2", "taskInputSha256": "a" * 64,
        "operationId": "unconfirmed-reserve", "operation": "reserve", "leaseOwner": "first", "attemptCount": 7}
    store.begin(operation)
    reopened = RemoteBudgetOperationStore(path, task_id="candidate-test", input_sha256="a" * 64)
    assert reopened.bind_candidate_attempt(owner="second", generation=8, resume=True) == 2
    assert reopened.pending() == operation
    # A rejected third invocation rolls back without losing either history or
    # the original operation identity. It is not another execution permission.
    with pytest.raises(ValueError, match="candidate_attempt_resume_unproved_or_exhausted"):
        reopened.bind_candidate_attempt(owner="third", generation=9, resume=True)
    assert reopened.pending() == operation
    assert reopened.candidate_attempt(owner="second", generation=8) == 2


def test_corrupt_candidate_history_fails_closed(tmp_path):
    import sqlite3
    from trading_desk.resource_budget_store import RemoteBudgetOperationStore
    path = tmp_path / "corrupt.sqlite"
    store = RemoteBudgetOperationStore.initialize(path, task_id="candidate-test", input_sha256="a" * 64)
    store.bind_candidate_attempt(owner="first", generation=7, resume=False)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE candidate_attempt SET generation=-1 WHERE ordinal=1")
    with pytest.raises(ValueError, match="candidate_attempt_history_corrupt"):
        store.candidate_attempt(owner="first", generation=7)
    with pytest.raises(ValueError, match="candidate_attempt_history_corrupt"):
        store.bind_candidate_attempt(owner="second", generation=8, resume=True)


def test_lease_profile_retains_old_client_contract_without_candidate_history():
    from trading_desk.pipeline_lease import OrdinaryPipelineLease
    from trading_desk.pipeline_checkpoint_preflight import ResumeCandidateRequestProfile
    args = dict(base_url="http://127.0.0.1:1", ingest_key="fixture-only",
        access_client_id="fixture-id", access_client_secret="fixture-secret",
        expected_attempt_count=8, allow_loopback_fixture=True,
        request_profile=ResumeCandidateRequestProfile("a" * 64, 7))
    with pytest.raises(PipelineRunnerError, match="pipeline_lease_request_profile_invalid"):
        OrdinaryPipelineLease(**args)
    with pytest.raises(PipelineRunnerError, match="pipeline_lease_request_profile_invalid"):
        OrdinaryPipelineLease(**args, candidate_attempt_ordinal=True)
    OrdinaryPipelineLease(**args, candidate_attempt_ordinal=2)
