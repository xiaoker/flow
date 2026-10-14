"""Explicit one-shot preparation through existing control APIs, not a scheduler.

The caller confirms a bounded non-secret package. Server authentication, CAS,
immutable approval and live lease guards remain authoritative. An ambiguous
write retains its original identity and stops; this entry does not auto-resume.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

from .http import HttpSettings, build_session
from .pipeline_application import canonical_sha256
from .pipeline_entry import (candidate_credentials, load_candidate_cost_policy, prepare_core_candidate,
    read_entry_request, reject_inline_credentials, verify_clean_candidate_code, verify_candidate_runtime,
    candidate_preparation_cost_upper, CANDIDATE_PREPARATION_CONTRACT, MAX_CANDIDATE_PREPARATION_REQUESTS)
from .pipeline_checkpoint_preflight import FreshRecoveryCandidateRequestProfile
from .pipeline_recovery_scope import bind_recovery_runner_scope
from .pipeline_runner import PipelineRunnerError, _assert_safe_output, _write_json_atomic, runner_budget_input_sha256
from .recovery_execution import plan_bytes
from .resource_budget import BudgetVector, RESOURCE_DIMENSIONS

CONTRACT = CANDIDATE_PREPARATION_CONTRACT
# Exact sequence: status, approval absence, budget preview, budget commit,
# immutable registration, registration readback, claim, status readback.
MAX_REQUESTS = MAX_CANDIDATE_PREPARATION_REQUESTS
MAX_RESPONSE_BYTES = 524288
MAX_PACKAGE_BYTES = 65536


def _require(condition, code):
    if not condition:
        raise PipelineRunnerError(code)


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(result.tzinfo is not None, "preparation_time_invalid")
        return result
    except (AttributeError, TypeError, ValueError):
        raise PipelineRunnerError("preparation_time_invalid") from None


def validate_preparation(package, confirmation, payload, *, snapshot_path, approved_plan,
                         producer_build_sha, project_root, now):
    reject_inline_credentials(package)
    _require(len(json.dumps(package).encode()) <= MAX_PACKAGE_BYTES, "preparation_package_size_invalid")
    _require(isinstance(package, dict) and set(package) == {"contract", "producerBuildSha", "requestSha256",
        "snapshotSha256", "planSha256", "scopeSha256", "expiresAt", "accountReconciliation", "registration"},
        "preparation_package_invalid")
    _require(package["contract"] == CONTRACT and canonical_sha256(package) == confirmation,
        "preparation_confirmation_mismatch")
    _require(package["producerBuildSha"] == producer_build_sha
        and package["requestSha256"] == canonical_sha256(payload), "preparation_request_binding_invalid")
    expiry = _instant(package["expiresAt"])
    _require(now < expiry and (expiry - now).total_seconds() <= 600, "preparation_deadline_invalid")
    root = Path(project_root).resolve()
    request, context, manifest = prepare_core_candidate(payload, snapshot_path=snapshot_path,
        producer_build_sha=producer_build_sha, dependency_lock_paths=[root / "web/package-lock.json",
        root / "config/pipeline-python-runtime.lock"], project_root=root, now=now)
    _require(_instant(context.lease_expires_at) <= expiry, "preparation_lease_deadline_invalid")
    _require(context.mode == "recovery" and approved_plan is not None, "preparation_recovery_required")
    bind_recovery_runner_scope(approved_plan, context, request["stages"], now=now)
    encoded_plan, scope_sha = plan_bytes(approved_plan)
    _require(approved_plan["dataDates"] == [context.identity.target_date]
        and set(approved_plan["requiredStages"]) == {"dashboard", "publish"}
        and approved_plan["maxDates"] == 1, "preparation_single_core_date_required")
    _require(package["snapshotSha256"] == payload["stageConfigs"]["build"]["snapshotSha256"]
        and package["planSha256"] == context.recovery_plan_sha256 and package["scopeSha256"] == scope_sha,
        "preparation_source_or_plan_binding_invalid")
    registration = package["registration"]
    _require(isinstance(registration, dict) and set(registration) == {"expectedAttemptCount", "expectedUpdatedAt",
        "previousStateJson", "reasonCode", "approvalReference", "authorizedAt", "expiresAt"},
        "preparation_registration_invalid")
    attempt = registration["expectedAttemptCount"]
    _require(type(attempt) is int and 0 <= attempt < 999999999
        and context.lease_attempt_count == attempt + 1, "preparation_generation_binding_invalid")
    _require(_instant(registration["authorizedAt"]) <= now < expiry
        <= _instant(registration["expiresAt"]) <= _instant(approved_plan["deadline"]), "preparation_approval_time_invalid")
    _instant(registration["expectedUpdatedAt"])
    try:
        previous = json.loads(registration["previousStateJson"])
    except (TypeError, ValueError):
        raise PipelineRunnerError("preparation_previous_state_invalid") from None
    _require(isinstance(previous, dict), "preparation_previous_state_invalid")
    reject_inline_credentials(previous)
    reconcile = package["accountReconciliation"]
    _require(isinstance(reconcile, dict) and reconcile.get("operation") == "account_reconcile"
        and reconcile.get("dryRun") is False and reconcile.get("evidenceContract") in
        {"flow-account-evidence-v2", "flow-account-evidence-v3"}, "preparation_account_contract_invalid")
    _require(isinstance(reconcile.get("operationId"), str) and re.fullmatch(r"[a-f0-9]{64}", reconcile["operationId"]) is not None,
        "preparation_account_contract_invalid")
    profile = FreshRecoveryCandidateRequestProfile(runner_budget_input_sha256(context, request["stages"], request["stageInputs"]),
        len(manifest["artifacts"]), recovery_id=context.recovery_id, plan_sha256=context.recovery_plan_sha256)
    _require(approved_plan["budgetManifest"].get("candidatePreparationContract") == CONTRACT,
        "preparation_plan_contract_invalid")
    policy = load_candidate_cost_policy(root, profile=profile, approved_recovery_plan=approved_plan)
    # Reuse the reviewed worst-operation policies, not caller-supplied costs.
    # Preparation shares the same ten-minute job already counted by runtime.
    upper = candidate_preparation_cost_upper(policy)
    total = policy["task"]
    _require(approved_plan["maxRequests"] >= total.metrics["workerRequests"].value,
        "preparation_request_scope_understated")
    config = json.loads((root / "config/resource-budget.json").read_bytes())
    names = {"d1RowsRead": "d1RowsReadPerDay", "d1RowsWritten": "d1RowsWrittenPerDay",
        "workerRequests": "workersRequestsPerDay", "actionsMinutes": "githubActionsMinutesPerMonth",
        "r2ClassA": "r2ClassA", "r2ClassB": "r2ClassB"}
    try:
        approved = BudgetVector.from_mapping(approved_plan["budgetManifest"]["taskCostUpperBound"])
        evidence = reconcile["evidence"]
        for d in RESOURCE_DIMENSIONS:
            used = evidence[d]["value"]
            cap = config["storage"]["r2SoftBytes"] if d == "r2Bytes" else min(config["limits"][names[d]], config["hardLimits"][names[d]])
            _require(type(used) is int and used >= 0 and approved.metrics[d].value is not None
                and approved.metrics[d].value >= total.metrics[d].value, "preparation_budget_unknown_or_understated")
            _require(used + total.metrics[d].value <= cap, "preparation_budget_exceeds_policy")
    except PipelineRunnerError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PipelineRunnerError("preparation_budget_unknown_or_understated") from None
    return context, encoded_plan, previous, upper, total


def prepare_candidate_authority(package, confirmation, payload, *, snapshot_path, approved_plan,
                                producer_build_sha, project_root, output_path, environment,
                                now=None, session=None, allow_loopback_fixture=False):
    current = now or datetime.now(timezone.utc)
    root = Path(project_root).resolve()
    origin = environment.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com")
    parsed = urlparse(origin)
    fixture = allow_loopback_fixture is True and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    _require((parsed.scheme == "https" and parsed.hostname == "flow.xiaoker.com" and parsed.port in {None, 443}) or fixture,
        "preparation_origin_invalid")
    _require(not (parsed.username or parsed.password or parsed.query or parsed.fragment) and parsed.path in {"", "/"},
        "preparation_origin_invalid")
    if not fixture:
        _require(Path(__file__).resolve() == root / "src/trading_desk/pipeline_preparation.py", "preparation_checkout_invalid")
        verify_clean_candidate_code(root)
    verify_candidate_runtime(root, [root / "web/package-lock.json", root / "config/pipeline-python-runtime.lock"])
    context, plan_data, previous, upper, total = validate_preparation(package, confirmation, payload,
        snapshot_path=snapshot_path, approved_plan=approved_plan, producer_build_sha=producer_build_sha,
        project_root=root, now=current)
    creds = candidate_credentials(environment)
    headers = {"Authorization": "Bearer " + creds["ingest_key"], "CF-Access-Client-Id": creds["access_client_id"],
        "CF-Access-Client-Secret": creds["access_client_secret"]}
    transport = session or build_session(HttpSettings(total_retries=0))
    _require(all(getattr(getattr(a, "max_retries", None), "total", None) in {0, False}
        for a in transport.adapters.values()), "preparation_hidden_retries_forbidden")
    output = Path(output_path)
    _assert_safe_output(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    report = {"contract": CONTRACT, "packageSha256": confirmation, "status": "PREPARING", "requests": 0,
        "steps": [], "estimatedPreparationUpperBound": upper.as_dict(), "combinedUpperBound": total.as_dict(),
        "actualProviderUsageKnown": False, "remoteWritesPossible": False,
        "candidateExecuted": False, "activeReleaseChanged": False, "sourceCollected": False}
    def save():
        _write_json_atomic(output, report)
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    save()

    def call(step, method, path, *, body=None, params=None):
        response = None
        _require(report["requests"] < MAX_REQUESTS and
            (fixture or datetime.now(timezone.utc) < _instant(package["expiresAt"])), "preparation_request_or_time_limit")
        record = {"step": step, "status": "PENDING", "method": method,
            "operationId": package["accountReconciliation"]["operationId"] if step.startswith("account") else context.recovery_plan_sha256}
        report["steps"].append(record)
        report["requests"] += 1
        report["remoteWritesPossible"] |= method == "POST"
        save()  # Retain exact intent before network; never retry a write.
        try:
            response = transport.request(method, origin.rstrip("/") + path, headers=headers, json=body, params=params,
                timeout=(8, 20), allow_redirects=False, stream=True)
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                _require(len(data) <= MAX_RESPONSE_BYTES, "preparation_response_limit")
            value = json.loads(data)
            _require(isinstance(value, dict), "preparation_response_invalid")
            record["httpStatus"] = response.status_code
            if response.status_code not in {200, 201, 404}:
                raise PipelineRunnerError("preparation_control_rejected")
            record["status"] = "RECEIVED"
            for name in ("rowsRead", "rowsWritten"):
                metric = value.get(name)
                record[name] = metric if type(metric) is int and metric >= 0 else None
            save()
            return response.status_code, value
        except PipelineRunnerError:
            raise
        except Exception:
            raise PipelineRunnerError("preparation_transport_or_payload_unknown") from None
        finally:
            if response is not None:
                response.close()

    try:
        key = context.identity.run_key
        code, window = call("window_preflight", "GET", "/api/v1/admin/pipeline-window/status", params={"runKey": key})
        expected = package["registration"]
        _require(code == 200 and window == previous and window.get("runKey") == key
            and window.get("targetDate") == context.identity.target_date and window.get("status") == "COMPLETED"
            and window.get("attemptCount") == expected["expectedAttemptCount"]
            and window.get("updatedAt") == expected["expectedUpdatedAt"]
            and window.get("leaseOwner") is None and window.get("recoveryId") is None, "preparation_window_changed")
        code, absent = call("approval_absence", "GET", "/api/v1/admin/pipeline-window/recovery",
            params={"recoveryId": context.recovery_id})
        _require(code == 404 and absent.get("error") == "recovery_not_found", "preparation_recovery_not_new_or_disabled")
        account = package["accountReconciliation"]
        code, preview = call("account_preview", "POST", "/api/v1/admin/pipeline-window/budget", body={**account, "dryRun": True})
        _require(code == 200 and preview.get("decision") == "account_reconciliation_prepared"
            and preview.get("expectedRevision") == account["expectedRevision"] and preview.get("executionAllowed") is False,
            "preparation_account_preview_invalid")
        receipt_identity = {"scope": "flow-account-v1", "epochDay": current.strftime("%Y-%m-%d"),
            "epochMonth": current.strftime("%Y-%m"), "evidenceContract": account["evidenceContract"],
            "evidenceReference": account["evidenceReference"], "evidenceSha256": canonical_sha256(account["evidence"])}
        _require(all(preview.get(k) == v for k, v in receipt_identity.items())
            and preview.get("remoteWrites") is False, "preparation_account_preview_invalid")
        code, reconciled = call("account_commit", "POST", "/api/v1/admin/pipeline-window/budget", body=account)
        _require(code == 200 and reconciled.get("decision") == "account_reconciled" and reconciled.get("scope") == "flow-account-v1"
            and reconciled.get("evidenceReference") == account["evidenceReference"]
            and reconciled.get("evidenceSha256") == canonical_sha256(account["evidence"])
            and reconciled.get("executionAllowed") is False and reconciled.get("replayed") is False,
            "preparation_account_receipt_invalid")
        expected_revision = 0 if account["expectedRevision"] is None else account["expectedRevision"] + 1
        _require(all(reconciled.get(k) == v for k, v in receipt_identity.items())
            and type(reconciled.get("revision")) is int and reconciled["revision"] == expected_revision,
            "preparation_account_receipt_invalid")
        registration = {**expected, "recoveryId": context.recovery_id, "runKey": key,
            "planSha256": context.recovery_plan_sha256, "scopeSha256": package["scopeSha256"],
            "planJson": plan_data.decode()}
        code, registered = call("register", "POST", "/api/v1/admin/pipeline-window/recovery", body=registration)
        _require(code == 201 and registered.get("decision") == "registered" and registered.get("status") == "AUTHORIZED"
            and registered.get("runKey") == key and registered.get("recoveryId") == context.recovery_id
            and registered.get("planStorageDecision") in {"stored", "already_stored"}, "preparation_registration_receipt_invalid")
        code, proof = call("registration_readback", "GET", "/api/v1/admin/pipeline-window/recovery",
            params={"recoveryId": context.recovery_id})
        _require(code == 200 and proof.get("status") == "AUTHORIZED" and all(proof.get(k) == v for k, v in
            {**expected, "recoveryId": context.recovery_id, "runKey": key, "planSha256": context.recovery_plan_sha256,
             "scopeSha256": package["scopeSha256"]}.items()), "preparation_registration_readback_invalid")
        code, claim = call("claim", "POST", "/api/v1/admin/pipeline-window/recovery/claim",
            body={"recoveryId": context.recovery_id, "runKey": key, "leaseOwner": context.lease_owner,
                  "sourceFingerprint": package["snapshotSha256"]})
        _require(code == 200 and claim.get("decision") == "acquired" and claim.get("status") == "DISPATCHED"
            and claim.get("recoveryId") == context.recovery_id and claim.get("runKey") == key
            and claim.get("leaseOwner") == context.lease_owner and claim.get("attemptCount") == context.lease_attempt_count,
            "preparation_claim_not_acquired")
        code, live = call("lease_readback", "GET", "/api/v1/admin/pipeline-window/status", params={"runKey": key})
        _require(code == 200 and all(live.get(k) == v for k, v in {"runKey": key, "windowDate": context.identity.window_date,
            "targetDate": context.identity.target_date, "leaseOwner": context.lease_owner, "attemptCount": context.lease_attempt_count,
            "recoveryId": context.recovery_id}.items()) and live.get("status") == "DISPATCHED"
            and live.get("sourceFingerprint") == package["snapshotSha256"]
            and claim.get("leaseExpiresAt") == live.get("leaseExpiresAt")
            and _instant(live.get("leaseExpiresAt")) >= _instant(context.lease_expires_at), "preparation_lease_readback_invalid")
        report["status"] = "CANDIDATE_AUTHORITY_PREPARED"
        report["claim"] = {k: claim[k] for k in ("recoveryId", "runKey", "leaseOwner", "attemptCount", "leaseExpiresAt")}
        save()
        return report
    except PipelineRunnerError as error:
        report["status"] = "UNKNOWN" if report["remoteWritesPossible"] else "BLOCKED"
        report["errorCode"] = error.code
        save()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--producer-build-sha", required=True)
    parser.add_argument("--run-candidate", action="store_true", help="Run the fixed candidate once after successful preparation")
    args = parser.parse_args()
    try:
        raw = os.environ.get("FLOW_ENTRY_PREPARATION_JSON", "")
        _require(0 < len(raw.encode()) <= MAX_PACKAGE_BYTES, "preparation_package_size_invalid")
        package = json.loads(raw)
        root = Path.cwd()
        result = prepare_candidate_authority(package, os.environ.get("FLOW_ENTRY_PREPARATION_SHA256", ""),
            read_entry_request(args.source_directory / "request.json"), snapshot_path=args.source_directory / "dashboard-snapshot.json",
            approved_plan=read_entry_request(args.source_directory / "approved-plan.json"), producer_build_sha=args.producer_build_sha,
            project_root=root, output_path=args.output, environment=os.environ)
        if args.run_candidate:
            from .pipeline_entry import run_candidate_entry
            result.pop("candidateExecuted", None)
            result["candidateInvocationStarted"] = True
            result["candidateOutcome"] = "UNKNOWN"
            _write_json_atomic(args.output, result)
            candidate = run_candidate_entry(read_entry_request(args.source_directory / "request.json"),
                snapshot_path=args.source_directory / "dashboard-snapshot.json", producer_build_sha=args.producer_build_sha,
                dependency_lock_paths=[root / "web/package-lock.json", root / "config/pipeline-python-runtime.lock"],
                project_root=root, state_dir=args.output.parent / "state", output_path=args.output.parent / "result.json",
                budget_journal=args.output.parent / "budget.sqlite", cost_policy=None, environment=os.environ,
                approved_recovery_plan=read_entry_request(args.source_directory / "approved-plan.json"))
            result["candidateOutcome"] = candidate["status"]
            result["stageExecutionStatus"] = candidate.get("stageExecutionStatus")
            _write_json_atomic(args.output, result)
            _write_json_atomic(args.output.parent / "summary.json", candidate)
            print(json.dumps({"status": candidate["status"], "packageSha256": result["packageSha256"],
                "requests": result["requests"], "stageExecutionStatus": candidate.get("stageExecutionStatus")}))
            if candidate["status"] != "COMPLETED":
                raise SystemExit(2)  # UNKNOWN retains the whole combined hold.
            return
        print(json.dumps({"status": result["status"], "packageSha256": result["packageSha256"], "requests": result["requests"]}))
    except (PipelineRunnerError, OSError, ValueError, TypeError, RecursionError) as error:
        print(json.dumps({"status": "PREPARATION_STOPPED", "errorCode": error.code if isinstance(error, PipelineRunnerError)
            else "preparation_local_input_invalid"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
