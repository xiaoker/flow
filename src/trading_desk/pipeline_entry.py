"""Trusted candidate entry preparation; dry-run is never execution authority."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import re
import subprocess
from pathlib import Path

from .pipeline_application import build_application_plan
from .pipeline_runner import (PipelineRunnerError, build_execution_context,
    dependency_lock_digest, _stage_names, _stage_inputs)

MAX_REQUEST_BYTES = 65536
MAX_SOURCE_ARCHIVE_BYTES = 6 * 1024 * 1024
_SECRET_KEYS = {"token", "secret", "password", "apikey", "ingestapikey", "clientsecret",
                "cfaccessclientsecret", "flowingestapikey", "flowcfaccessclientsecret",
                "accesstoken", "refreshtoken", "githubtoken", "privatekey"}


def read_entry_request(path: Path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise PipelineRunnerError("entry_request_path_invalid")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_REQUEST_BYTES + 1)
    except OSError:
        raise PipelineRunnerError("entry_request_unavailable") from None
    if len(data) > MAX_REQUEST_BYTES:
        raise PipelineRunnerError("entry_request_size_exceeded")
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise PipelineRunnerError("entry_request_json_invalid") from None
    if not isinstance(value, dict):
        raise PipelineRunnerError("entry_request_object_required")
    reject_inline_credentials(value)
    return value


def read_environment_request(environment, name="FLOW_ENTRY_REQUEST_JSON", *, required=True):
    raw = environment.get(name, "")
    if not raw and not required:
        return None
    if not isinstance(raw, str) or len(raw.encode()) > MAX_REQUEST_BYTES:
        raise PipelineRunnerError("entry_request_size_exceeded")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise PipelineRunnerError("entry_request_json_invalid") from None
    if not isinstance(value, dict):
        raise PipelineRunnerError("entry_request_object_required")
    reject_inline_credentials(value)
    return value


def prepare_declared_request(destination, *, environment):
    """Validate the actual proposed dry-run input without contacting a server."""
    from .pipeline_runner import _assert_safe_output
    value = read_environment_request(environment)
    root = Path(destination)
    _assert_safe_output(root / "request.json")
    if root.exists():
        raise PipelineRunnerError("entry_input_destination_exists")
    root.mkdir(parents=True, mode=0o700)
    with (root / "request.json").open("xb") as stream:
        os.chmod(root / "request.json", 0o600)
        stream.write(json.dumps(value).encode())
    return {"status": "DECLARED_REQUEST_PREPARED", "serverAuthorityVerified": False}


def prepare_workflow_inputs(destination, *, artifact_id, source_run_id, environment):
    """Read one exact GitHub artifact; never enumerate, extract all, or dispatch.

    Inputs are written only after complete validation. GitHub credentials are
    environment-only and never forwarded to the signed archive host.
    """
    import io
    import zipfile
    from urllib.parse import urlparse
    from .bounded_http import BoundedHTTPExchange, BoundedHTTPError
    from .http import build_session, HttpSettings
    from .pipeline_runner import _assert_safe_output
    if any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
           for value in (artifact_id, source_run_id)):
        raise PipelineRunnerError("entry_source_reference_invalid")
    request = read_environment_request(environment)
    plan = read_environment_request(environment, "FLOW_ENTRY_RECOVERY_PLAN_JSON", required=False)
    token = environment.get("GITHUB_TOKEN")
    if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
        raise PipelineRunnerError("entry_source_authentication_required")
    root = Path(destination)
    _assert_safe_output(root / "request.json")
    if root.exists():
        raise PipelineRunnerError("entry_input_destination_exists")
    endpoint = f"https://api.github.com/repos/xiaoker/flow/actions/artifacts/{artifact_id}"
    session = build_session(HttpSettings(total_retries=0))
    session.trust_env = False  # No ambient netrc authentication on the signed host.
    session.auth = None
    exchange = BoundedHTTPExchange(session)
    def get(url, *, auth=False, maximum=16384):
        reply = exchange.request("GET", url, max_bytes=maximum, chunk_size=65536,
            max_chunks=max(2, maximum // 65536 + 1), deadline_seconds=20,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if auth else {})
        return reply
    try:
        metadata_reply = get(endpoint, auth=True)
        if metadata_reply.status != 200:
            raise PipelineRunnerError("entry_source_metadata_unavailable")
        metadata = json.loads(metadata_reply.content)
        if (not isinstance(metadata, dict) or metadata.get("id") != int(artifact_id)
                or metadata.get("expired") is not False
                or metadata.get("name") not in {f"market-data-collection-source-{source_run_id}", f"market-data-recovery-source-{source_run_id}"}
                or not isinstance(metadata.get("workflow_run"), dict)
                or metadata["workflow_run"].get("id") != int(source_run_id)
                or type(metadata.get("size_in_bytes")) is not int
                or not 0 < metadata["size_in_bytes"] <= MAX_SOURCE_ARCHIVE_BYTES):
            raise PipelineRunnerError("entry_source_metadata_mismatch")
        redirect = get(endpoint + "/zip", auth=True)
        signed = redirect.headers.get("Location", "")
        parsed = urlparse(signed)
        if (redirect.status != 302 or parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith((".blob.core.windows.net", ".githubusercontent.com"))
                or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
            raise PipelineRunnerError("entry_source_archive_origin_invalid")
        archive_reply = get(signed, maximum=MAX_SOURCE_ARCHIVE_BYTES)
        if archive_reply.status != 200:
            raise PipelineRunnerError("entry_source_archive_unavailable")
        with zipfile.ZipFile(io.BytesIO(archive_reply.content)) as archive:
            entries = archive.infolist()
            if len(entries) > 64:
                raise PipelineRunnerError("entry_source_archive_limit")
            for entry in entries:
                name = entry.filename
                if (name.startswith("/") or "\\" in name or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/"))
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise PipelineRunnerError("entry_source_archive_path_invalid")
            matches = [entry for entry in entries if entry.filename == "dashboard-snapshot.json"
                or entry.filename.endswith("/source-bundle/dashboard-snapshot.json")]
            if len(matches) != 1 or not 0 < matches[0].file_size <= 4 * 1024 * 1024:
                raise PipelineRunnerError("entry_source_snapshot_unavailable")
            with archive.open(matches[0]) as stream:
                snapshot = stream.read(4 * 1024 * 1024 + 1)
            if len(snapshot) != matches[0].file_size:
                raise PipelineRunnerError("entry_source_snapshot_size_mismatch")
        value = json.loads(snapshot)
        reject_inline_credentials(value)
        if (not isinstance(value, dict) or value.get("asOf") != request.get("identity", {}).get("targetDate")
                or hashlib.sha256(snapshot).hexdigest() != request.get("stageConfigs", {}).get("build", {}).get("snapshotSha256")):
            raise PipelineRunnerError("entry_source_snapshot_binding_mismatch")
    except PipelineRunnerError:
        raise
    except (BoundedHTTPError, ValueError, OSError, KeyError, AttributeError, zipfile.BadZipFile, RuntimeError):
        raise PipelineRunnerError("entry_source_read_failed") from None
    finally:
        session.close()
    root.mkdir(parents=True, mode=0o700)
    for name, data in {"request.json": json.dumps(request).encode(), "dashboard-snapshot.json": snapshot,
                       **({"approved-plan.json": json.dumps(plan).encode()} if plan else {})}.items():
        with (root / name).open("xb") as stream:
            os.chmod(root / name, 0o600)
            stream.write(data)
    return {"status": "PINNED_SOURCE_PREPARED", "artifactId": artifact_id, "sourceRunId": source_run_id,
        "snapshotSha256": hashlib.sha256(snapshot).hexdigest(), "serverAuthorityVerified": False}


def reject_inline_credentials(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise PipelineRunnerError("entry_request_depth_exceeded")
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).replace("_", "").replace("-", "").lower()
                if normalized in _SECRET_KEYS:
                    raise PipelineRunnerError("inline_credential_forbidden")
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _checkout_environment():
    # Git repository overrides must not redirect identity or clean-code checks.
    # Preserve unrelated environment settings without changing the caller.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def verify_checkout_sha(root: Path, producer_sha: str):
    if not isinstance(producer_sha, str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", producer_sha):
        raise PipelineRunnerError("entry_checkout_sha_invalid")
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
            env=_checkout_environment(), capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PipelineRunnerError("entry_checkout_unavailable") from None
    if result.returncode != 0 or result.stdout.strip() != producer_sha.encode():
        raise PipelineRunnerError("entry_checkout_sha_mismatch")


def dry_run_entry(payload, *, producer_build_sha, dependency_lock_paths, project_root, now=None):
    root = Path(project_root).resolve()
    verify_checkout_sha(root, producer_build_sha)
    locks = dependency_lock_digest(dependency_lock_paths, root=root)
    context = build_execution_context(payload, producer_build_sha=producer_build_sha,
        dependency_lock_sha256=locks["sha256"], now=now)
    stages = _stage_names(payload)
    plan = build_application_plan(context.identity, stages,
        stage_inputs=_stage_inputs(payload, stages))
    return {"status": "DECLARED_INPUT_CHECKED", "executionMode": "dry-run",
        "identity": context.identity.as_dict(), "planHash": plan["planHash"],
        "liveExecutionAllowed": False, "serverAuthorityVerified": False,
        "sideEffects": {"remoteWrites": False, "workflowDispatch": False,
                        "activeReleaseChanged": False, "filesystemWrites": False}}


def prepare_core_candidate(payload, *, snapshot_path, producer_build_sha,
                           dependency_lock_paths, project_root, now=None):
    """Derive the fixed core graph using existing builders, without effects.

    This does not accept stageResults, custom adapters or inferred dates.
    A supplied graph must match the graph derived from the pinned source.
    The caller still needs live server authorization and whole-task admission.
    """
    from .pipeline_application import canonical_sha256
    from .pipeline_build_adapter import CoreReadModelBuildAdapter, _read, _result, MAX_INPUT_BYTES
    from .pipeline_adapters import PipelineAdapterError
    reject_inline_credentials(payload)
    verify_checkout_sha(Path(project_root).resolve(), producer_build_sha)
    if "stageResults" in payload or payload.get("stages", ["build", "verify", "publish"]) != ["build", "verify", "publish"]:
        raise PipelineRunnerError("entry_candidate_graph_required")
    locks = dependency_lock_digest(dependency_lock_paths, root=Path(project_root).resolve())
    context = build_execution_context(payload, producer_build_sha=producer_build_sha,
        dependency_lock_sha256=locks["sha256"], now=now)
    path = Path(snapshot_path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PipelineRunnerError("entry_source_path_invalid")
    data = _read(path, MAX_INPUT_BYTES)
    try:
        source_value = json.loads(data)
    except (ValueError, RecursionError):
        raise PipelineRunnerError("entry_source_json_invalid") from None
    reject_inline_credentials(source_value)
    supplied = payload.get("stageConfigs", {})
    if not isinstance(supplied, dict) or not isinstance(supplied.get("build"), dict):
        raise PipelineRunnerError("entry_stage_config_mismatch")
    pinned = supplied.get("build", {}).get("snapshotSha256")
    if not isinstance(pinned, str) or pinned != hashlib.sha256(data).hexdigest():
        raise PipelineRunnerError("entry_source_hash_mismatch")
    build_config = {"snapshotSha256": pinned}
    build_input = canonical_sha256(build_config)
    builder = CoreReadModelBuildAdapter(path)
    encoded, manifest = builder._expected(context.identity, build_config, build_input, data)
    if manifest.get("publication", {}).get("status") != "confirmed":
        raise PipelineAdapterError("verify_publication_not_confirmed")
    built = _result(encoded, manifest)
    verify_config = {"buildInputSha256": build_input, "buildOutputSha256": built["outputSha256"]}
    verify_input = canonical_sha256(verify_config)
    verify_output = canonical_sha256({"buildOutputSha256": built["outputSha256"], "inputSha256": verify_input})
    configs = {"build": build_config, "verify": verify_config,
        "publish": {"verifyInputSha256": verify_input, "verifyOutputSha256": verify_output}}
    if any(name not in configs or value != configs[name] for name, value in supplied.items()):
        raise PipelineRunnerError("entry_stage_config_mismatch")
    inputs = {name: canonical_sha256(value) for name, value in configs.items()}
    if "stageInputs" in payload and payload["stageInputs"] != inputs:
        raise PipelineRunnerError("entry_stage_input_mismatch")
    request = {**payload, "stages": ["build", "verify", "publish"], "stageConfigs": configs, "stageInputs": inputs}
    return request, context, manifest


def candidate_credentials(environment):
    """Only fixed environment keys; never include their values in errors."""
    names = {"ingest_key": "FLOW_INGEST_API_KEY", "access_client_id": "FLOW_CF_ACCESS_CLIENT_ID",
             "access_client_secret": "FLOW_CF_ACCESS_CLIENT_SECRET"}
    values = {argument: environment.get(name) for argument, name in names.items()}
    if any(not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value
           for value in values.values()):
        raise PipelineRunnerError("entry_environment_credentials_required")
    return values


def load_candidate_cost_policy(project_root, *, profile=None):
    """Read only reviewed repository policy, never a request-supplied path.

    Daily limits and allocation targets do not establish operation upper bounds.
    An absent policy blocks live entry rather than manufacturing those bounds.
    """
    from .resource_budget import BudgetVector, RESOURCE_DIMENSIONS
    path = Path(project_root).resolve() / "config/resource-budget.json"
    document = read_entry_request(path)
    policy = document.get("resourceAccounting", {}).get("candidateEntryPolicy")
    if not isinstance(policy, dict) or policy.get("schemaVersion") != 1:
        raise PipelineRunnerError("entry_cost_policy_not_established")
    groups = {"stage": {"build", "verify", "publish"}, "control": {"lease", "budget"},
              "transport": {"candidate", "checkpoint"}}
    if set(policy) != {"schemaVersion", "evidence", "runtime", *groups}:
        raise PipelineRunnerError("entry_cost_policy_invalid")
    evidence = policy["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"basis", "reference"} or evidence["basis"] != "reviewed-upper-bound" or not isinstance(evidence["reference"], str) or not evidence["reference"].strip():
        raise PipelineRunnerError("entry_cost_policy_evidence_required")
    def vector(value):
        if not isinstance(value, dict) or set(value) != set(RESOURCE_DIMENSIONS):
            raise PipelineRunnerError("entry_cost_policy_invalid")
        try:
            result = BudgetVector.estimated(value, scope="reviewed_candidate_entry_upper")
        except (ValueError, TypeError):
            raise PipelineRunnerError("entry_cost_policy_invalid") from None
        if not result.all_known():
            raise PipelineRunnerError("entry_cost_policy_unknown")
        return result
    result = {"runtime": vector(policy["runtime"])}
    for group, names in groups.items():
        values = policy[group]
        if not isinstance(values, dict) or set(values) != names:
            raise PipelineRunnerError("entry_cost_policy_invalid")
        result[group] = {name: vector(values[name]) for name in names}
    from .pipeline_checkpoint_preflight import (candidate_task_cost_upper,
        FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile,
        ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile)
    if profile is None:
        raise PipelineRunnerError("entry_cost_profile_required")
    total_runtime = result["runtime"].add(result["runtime"], scope="two_attempt_runtime_upper")
    # Reserve both bounded attempts from the start. A takeover may not enlarge
    # the first hold or manufacture a new allowance to fund its tail.
    if type(profile) is FreshCandidateRequestProfile:
        total_profile = ResumeCandidateRequestProfile(profile.input_sha256, profile.artifact_count,
            maximum_seconds=profile.maximum_seconds)
    elif type(profile) is FreshRecoveryCandidateRequestProfile:
        total_profile = ResumeRecoveryCandidateRequestProfile(profile.input_sha256, profile.artifact_count,
            maximum_seconds=profile.maximum_seconds, recovery_id=profile.recovery_id, plan_sha256=profile.plan_sha256)
    elif type(profile) in {ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile}:
        total_profile = profile
        result["runtime"] = total_runtime
    else:
        raise PipelineRunnerError("entry_cost_profile_invalid")
    result["task"] = candidate_task_cost_upper(profile=total_profile, stage_costs=result["stage"],
        request_costs={**result["control"], **result["transport"]}, runtime_cost=total_runtime)
    return result


def verify_clean_candidate_code(root):
    """Live code/policy must be the checkout, not unreviewed working files."""
    paths = ["src/trading_desk", "scripts", "config", "web/package-lock.json"]
    try:
        tracked = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *paths], cwd=root,
                                 env=_checkout_environment(), capture_output=True, timeout=5)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", *paths],
                                   cwd=root, env=_checkout_environment(), capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise PipelineRunnerError("entry_checkout_unavailable") from None
    if tracked.returncode != 0 or untracked.returncode != 0 or untracked.stdout:
        raise PipelineRunnerError("entry_candidate_code_dirty")


def verify_candidate_runtime(root, dependency_lock_paths):
    """Bind both lock files and verify actual imported-runtime versions."""
    import importlib.metadata
    expected = {"web/package-lock.json", "config/pipeline-python-runtime.lock"}
    locks = dependency_lock_digest(dependency_lock_paths, root=root)
    if {entry["path"] for entry in locks["files"]} != expected or len(locks["files"]) != 2:
        raise PipelineRunnerError("entry_runtime_lock_set_required")
    if sys.version_info[:2] != (3, 12):
        raise PipelineRunnerError("entry_python_version_mismatch")
    path = root / "config/pipeline-python-runtime.lock"
    if path.is_symlink() or path.stat().st_size > MAX_REQUEST_BYTES:
        raise PipelineRunnerError("entry_python_lock_invalid")
    pins = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)==([0-9][A-Za-z0-9.+_-]*)", line)
        if not match:
            raise PipelineRunnerError("entry_python_lock_invalid")
        name, version = match.groups()
        key = name.lower().replace("_", "-")
        if key in pins:
            raise PipelineRunnerError("entry_python_lock_invalid")
        pins[key] = version
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise PipelineRunnerError("entry_python_runtime_incomplete") from None
        if actual != version:
            raise PipelineRunnerError("entry_python_runtime_mismatch")
    if len(pins) != 19:
        raise PipelineRunnerError("entry_python_lock_incomplete")


def run_candidate_entry(payload, *, snapshot_path, producer_build_sha, dependency_lock_paths,
                        project_root, state_dir, output_path, budget_journal, cost_policy,
                        environment, approved_recovery_plan=None, resume_handoff_sha256=None,
                        now=None, allow_loopback_fixture=False):
    """Fixed client/adapter assembly for a trusted caller, never plugin input.

    cost_policy comes from reviewed code policy, not execution-request numbers.
    The public CLI will not expose the loopback test seam. Missing actual usage
    is UNKNOWN and cannot release the server hold or be reported as success.
    """
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    from .resource_budget import BudgetVector
    from .resource_budget_store import RemoteBudgetOperationStore
    from .resource_budget_remote import SharedBudgetLedger, RecoverySharedBudgetLedger
    from .pipeline_lease import OrdinaryPipelineLease
    from .pipeline_recovery_lease import RecoveryPipelineLease
    from .pipeline_candidate_transport import CandidatePublicationTransport, RecoveryCandidatePublicationTransport
    from .pipeline_checkpoint_transport import CheckpointObjectTransport, RecoveryCheckpointObjectTransport
    from .pipeline_publication_intent import PublicationIntentStore
    from .pipeline_build_adapter import CoreReadModelBuildAdapter
    from .pipeline_verify_adapter import CoreReadModelVerifyAdapter
    from .pipeline_publish_adapter import CoreCandidatePublishAdapter
    from .pipeline_checkpoint_preflight import (FreshCandidateRequestProfile, FreshRecoveryCandidateRequestProfile,
        ResumeCandidateRequestProfile, ResumeRecoveryCandidateRequestProfile)
    from .pipeline_runner import _assert_safe_output, runner_budget_input_sha256, run_pipeline_request
    from .recovery_execution import plan_bytes
    root = Path(project_root).resolve()
    origin = environment.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com")
    parsed = urlparse(origin)
    fixture = allow_loopback_fixture is True and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if allow_loopback_fixture is not False and not fixture:
        raise PipelineRunnerError("entry_test_origin_invalid")
    if not fixture:
        if Path(__file__).resolve() != root / "src/trading_desk/pipeline_entry.py":
            raise PipelineRunnerError("entry_runtime_not_checkout_code")
        verify_clean_candidate_code(root)
    verify_candidate_runtime(root, dependency_lock_paths)
    request, context, manifest = prepare_core_candidate(payload, snapshot_path=snapshot_path,
        producer_build_sha=producer_build_sha, dependency_lock_paths=dependency_lock_paths,
        project_root=root, now=now)
    recovery = context.mode == "recovery"
    resume = resume_handoff_sha256 is not None
    if context.lease_attempt_count != (2 if resume else 1):
        raise PipelineRunnerError("entry_candidate_attempt_invalid")
    if recovery != (approved_recovery_plan is not None):
        raise PipelineRunnerError("entry_recovery_plan_binding_required")
    digest = runner_budget_input_sha256(context, request["stages"], request["stageInputs"])
    profile_type = ((ResumeRecoveryCandidateRequestProfile if resume else FreshRecoveryCandidateRequestProfile)
        if recovery else (ResumeCandidateRequestProfile if resume else FreshCandidateRequestProfile))
    profile = profile_type(digest, len(manifest["artifacts"]), **({"recovery_id": context.recovery_id,
        "plan_sha256": context.recovery_plan_sha256} if recovery else {}))
    if cost_policy is None:
        cost_policy = load_candidate_cost_policy(root, profile=profile)
    if not isinstance(cost_policy, dict) or set(cost_policy) != {"task", "stage", "control", "transport", "runtime"}:
        raise PipelineRunnerError("entry_cost_policy_required")
    try:
        vectors = [cost_policy["task"], cost_policy["runtime"], *cost_policy["stage"].values(),
                   *cost_policy["control"].values(), *cost_policy["transport"].values()]
    except (TypeError, AttributeError):
        raise PipelineRunnerError("entry_cost_policy_required") from None
    if (set(cost_policy["stage"]) != {"build", "verify", "publish"}
            or set(cost_policy["control"]) != {"lease", "budget"}
            or set(cost_policy["transport"]) != {"candidate", "checkpoint"}):
        raise PipelineRunnerError("entry_cost_policy_required")
    if any(not isinstance(v, BudgetVector) or not v.all_known() for v in vectors):
        raise PipelineRunnerError("entry_cost_policy_unknown")
    common = {"base_url": origin, **candidate_credentials(environment), "allow_loopback_fixture": fixture}
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    guard_args = {**common, "expected_attempt_count": context.lease_attempt_count, "now": clock, "request_profile": profile}
    guard = RecoveryPipelineLease(**guard_args, approved_plan=approved_recovery_plan, stages=request["stages"]) if recovery else OrdinaryPipelineLease(**guard_args)
    task = f"{context.identity.run_key}/{context.identity.target_date}/flow-budget-task-v2"
    if recovery:
        task = f"recovery/{context.recovery_id}/{context.recovery_plan_sha256}/{task}"
    state_dir, output_path, budget_journal = Path(state_dir), Path(output_path), Path(budget_journal)
    for path in (state_dir / "intent.sqlite", output_path, budget_journal):
        _assert_safe_output(path)
    if resume and not budget_journal.is_file():
        raise PipelineRunnerError("entry_resume_budget_journal_required")
    budget_journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_factory = RemoteBudgetOperationStore if budget_journal.exists() else RemoteBudgetOperationStore.initialize
    store = store_factory(budget_journal, task_id=task, input_sha256=digest)
    budget_args = {**common, "lease_guard": guard, "run_key": context.identity.run_key,
        "lease_owner": context.lease_owner, "attempt_count": context.lease_attempt_count,
        "target_date": context.identity.target_date, "server_fenced_checks": True,
        "task_input_sha256": digest, "operation_store": store, "request_profile": profile}
    ledger = RecoverySharedBudgetLedger(**budget_args, approved_plan=approved_recovery_plan, context=context,
        stages=request["stages"], now=clock()) if recovery else SharedBudgetLedger(**budget_args)
    execution = {"runKey": context.identity.run_key, "targetDate": context.identity.target_date,
        "producerBuildSha": producer_build_sha, "leaseOwner": context.lease_owner, "attemptCount": context.lease_attempt_count}
    if recovery:
        execution.update(recoveryId=context.recovery_id, planSha256=context.recovery_plan_sha256,
                         scopeSha256=plan_bytes(approved_recovery_plan)[1])
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    intent_path = state_dir / "intent.sqlite"
    intent_factory = PublicationIntentStore if intent_path.exists() else PublicationIntentStore.initialize
    intent = intent_factory(intent_path, execution=execution, release_id=manifest["releaseId"], manifest_sha256=manifest["manifestSha256"])
    transport_args = {**common, "lease_guard": guard, "budget_ledger": ledger, "budget_task_id": task}
    candidate_args = {**transport_args, "intent_store": intent,
                      "request_costs": {method: cost_policy["transport"]["candidate"] for method in ("GET", "POST")}}
    checkpoint_args = {**transport_args,
                       "request_costs": {method: cost_policy["transport"]["checkpoint"] for method in ("GET", "POST")}}
    candidate = RecoveryCandidatePublicationTransport(**candidate_args, approved_plan=approved_recovery_plan, now=clock) if recovery else CandidatePublicationTransport(**candidate_args)
    checkpoints = RecoveryCheckpointObjectTransport(**checkpoint_args, approved_plan=approved_recovery_plan,
        stages=request["stages"], now=clock) if recovery else CheckpointObjectTransport(**checkpoint_args)
    workspaces = {stage: state_dir / "stages" / stage / request["stageInputs"][stage] for stage in request["stages"]}
    adapters = {"build": CoreReadModelBuildAdapter(Path(snapshot_path)),
        "verify": CoreReadModelVerifyAdapter(workspaces["build"]),
        "publish": CoreCandidatePublishAdapter(workspaces["verify"], candidate, context, restore_completed_intents=resume)}
    return run_pipeline_request(request, state_dir=state_dir, output_path=output_path,
        producer_build_sha=producer_build_sha, dependency_lock_paths=dependency_lock_paths, project_root=root,
        now=now, stage_adapters=adapters, lease_guard=guard, budget_ledger=ledger,
        task_cost_upper=cost_policy["task"], stage_cost_upper=cost_policy["stage"],
        control_request_cost_upper=cost_policy["control"], runtime_cost_upper=cost_policy["runtime"],
        resource_measurement=lambda kind, stage: BudgetVector.unknown(reason="entry_provider_usage_unavailable"),
        export_bundles=True, export_handoff=True, checkpoint_transport=checkpoints,
        resume_handoff_sha256=resume_handoff_sha256,
        **({"approved_recovery_plan": approved_recovery_plan} if recovery else {}))
