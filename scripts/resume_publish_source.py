"""Claim an approved publish-only recovery using an exact retained artifact.

Uses only the existing recovery API. No dependencies, collection, dispatch,
budget reset or release mutation. An ambiguous write is never retried.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trading_desk.recovery_execution import validate_recovery_plan  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("publish_resume_redirect_refused")


def required(name):
    value = os.environ.get(name, "")
    if not value or "\n" in value or "\r" in value:
        raise ValueError("publish_resume_missing_" + name)
    return value


def request_json(url, headers, body=None, *, stage="request"):
    data = None if body is None else json.dumps(body).encode()
    # Identify the real application, not a browser or another client. Default
    # Python-urllib is rejected by the production edge before service auth.
    request = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json", **headers, "User-Agent": "flow-publish-resume/1.0"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(350_001)
    except urllib.error.HTTPError as error:
        # Only a numeric Cloudflare error is safe to report. Never emit URLs,
        # response headers/bodies, credentials, cookies or arbitrary API text.
        body = error.read(256).decode("utf-8", errors="replace")
        match = re.fullmatch(r"error code: ([0-9]{3,5})\s*", body)
        edge = "_edge_" + match[1] if match else ""
        outcome = "_write_outcome_unknown" if data is not None else ""
        raise ValueError(f"publish_resume_http_{stage}_{error.code}{edge}{outcome}") from None
    if len(raw) > 350_000:
        raise ValueError("publish_resume_response_limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("publish_resume_response_shape")
    return value


def future(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.tzinfo is not None and parsed > datetime.now(timezone.utc)


def resume():
    if required("PIPELINE_RECOVERY_ENABLED") not in {"1", "true"}:
        raise ValueError("publish_resume_disabled")
    recovery_id, run_key = required("INPUT_RECOVERY_ID"), required("INPUT_RUN_KEY")
    target = required("INPUT_RECOVERY_DATA_DATE")
    if required("INPUT_RECOVERY_STAGE") != "publish" or required("INPUT_TARGET_DATE") != target:
        raise ValueError("publish_resume_date_stage_mismatch")
    base = required("FLOW_PIPELINE_WINDOW_URL").rstrip("/")
    headers = {"Authorization": "Bearer " + required("FLOW_INGEST_API_KEY"),
               "CF-Access-Client-Id": required("FLOW_CF_ACCESS_CLIENT_ID"),
               "CF-Access-Client-Secret": required("FLOW_CF_ACCESS_CLIENT_SECRET"),
               "Content-Type": "application/json"}
    suffix = "?recoveryId=" + quote(recovery_id, safe="")
    state = request_json(base + "/recovery" + suffix, headers, stage="read_status")
    if state.get("recoveryId") != recovery_id or state.get("runKey") != run_key:
        raise ValueError("publish_resume_state_identity")
    if state.get("status") == "COMPLETED":
        return {"should_run": "false"}
    if state.get("status") not in {"AUTHORIZED", "ACTIVE", "PAUSED"} or not future(state.get("expiresAt")):
        raise ValueError("publish_resume_approval_unavailable")
    envelope = request_json(base + "/recovery/plan" + suffix, headers, stage="read_plan")
    plan = envelope.get("plan", {})
    expected = required("INPUT_RECOVERY_PLAN_SHA256")
    validate_recovery_plan(plan, expected_plan_hash=expected)
    if (envelope.get("recoveryId") != recovery_id or envelope.get("planSha256") != expected
            or plan.get("recoveryId") != recovery_id or plan.get("runKey") != run_key
            or target not in plan.get("dataDates", []) or "publish" not in plan.get("requiredStages", [])):
        raise ValueError("publish_resume_plan_identity")
    if not future(plan.get("deadline")):
        raise ValueError("publish_resume_plan_expired")
    source = plan.get("budgetManifest", {}).get("publishSource", {})
    if not isinstance(source, dict):
        raise ValueError("publish_resume_source_binding")
    for key in ("artifactId", "artifactBytes", "expectedAttemptCount"):
        if type(source.get(key)) is not int or source[key] < (0 if key == "expectedAttemptCount" else 1):
            raise ValueError("publish_resume_source_" + key)
    if (not re.fullmatch(r"[0-9a-f]{64}", str(source.get("artifactSha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sourceFingerprint", "")))
            or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("producerCommit", "")))
            or source.get("sourceRunId") != required("INPUT_SOURCE_RUN_ID")
            or source.get("artifactName") != required("INPUT_ARTIFACT_NAME")
            or source["artifactBytes"] > 32 * 1024 * 1024):
        raise ValueError("publish_resume_source_binding")
    pipeline = state.get("pipeline", {})
    if pipeline.get("status") in {"DISPATCHED", "RUNNING"} and future(pipeline.get("leaseExpiresAt")):
        return {"should_run": "false"}
    if pipeline.get("attemptCount") != source["expectedAttemptCount"]:
        raise ValueError("publish_resume_attempt_changed")
    repository = required("GITHUB_REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("publish_resume_repository")
    artifact = request_json(required("GITHUB_API_URL").rstrip("/") +
        f"/repos/{repository}/actions/artifacts/{source['artifactId']}",
        {"Authorization": "Bearer " + required("GH_TOKEN"), "Accept": "application/vnd.github+json"}, stage="artifact_metadata")
    workflow = artifact.get("workflow_run", {})
    if (artifact.get("id") != source["artifactId"] or artifact.get("name") != source["artifactName"]
            or artifact.get("size_in_bytes") != source["artifactBytes"] or artifact.get("expired") is not False
            or artifact.get("digest") != "sha256:" + source["artifactSha256"]
            or str(workflow.get("id")) != source["sourceRunId"]
            or workflow.get("head_sha") != source["producerCommit"]):
        raise ValueError("publish_resume_artifact_mismatch")
    owner = "github-publish:" + required("GITHUB_RUN_ID") + ":" + required("GITHUB_RUN_ATTEMPT")
    claim = request_json(base + "/recovery/claim", headers, {
        "recoveryId": recovery_id, "runKey": run_key, "leaseOwner": owner,
        "sourceFingerprint": source["sourceFingerprint"], "githubRunId": required("GITHUB_RUN_ID")}, stage="claim")
    if claim.get("decision") in {"already_satisfied", "skip_leased", "skip_blocked"}:
        return {"should_run": "false"}
    attempt = source["expectedAttemptCount"] + 1
    if (claim.get("decision") not in {"acquired", "takeover"} or claim.get("recoveryId") != recovery_id
            or claim.get("runKey") != run_key or claim.get("leaseOwner") != owner
            or claim.get("attemptCount") != attempt or not future(claim.get("leaseExpiresAt"))):
        raise ValueError("publish_resume_claim_unknown")
    # Existing gate requires RUNNING. Never retry this mutation after lost response.
    running = request_json(base + "/recovery/status", headers, {
        "recoveryId": recovery_id, "runKey": run_key, "leaseOwner": owner,
        "expectedAttemptCount": attempt, "githubRunId": required("GITHUB_RUN_ID"),
        "status": "RUNNING", "renewLease": True}, stage="mark_running")
    if (running.get("recoveryId") != recovery_id or running.get("runKey") != run_key
            or running.get("status") != "RUNNING" or running.get("attemptCount") != attempt
            or running.get("leaseOwner") != owner or not future(running.get("leaseExpiresAt"))):
        raise ValueError("publish_resume_running_unknown")
    return {"should_run": "true", "lease_owner": owner, "attempt_count": str(attempt)}


def main():
    try:
        outputs = resume()
        with Path(required("GITHUB_OUTPUT")).open("a") as stream:
            for key, value in outputs.items():
                if "\n" in value or "\r" in value:
                    raise ValueError("publish_resume_output")
                stream.write(f"{key}={value}\n")
    except Exception as error:
        # Do not echo provider response bodies, URLs or headers containing secrets.
        code = str(error) if str(error).startswith("publish_resume_") else type(error).__name__
        print("Publish-only resume failed closed: " + code, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
