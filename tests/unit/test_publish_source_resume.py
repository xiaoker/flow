"""The publish-only entry verifies a pinned source before any control write."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import textwrap
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from trading_desk.recovery_execution import build_recovery_plan

ROOT = Path(__file__).resolve().parents[2]


def test_ordinary_lineage_uses_only_observed_verified_objects():
    from scripts.resume_publish_source import ordinary_source_lineage
    item = {"status": "ready", "key": "market-data/normalized/sse/etf_scale/2026-11-26/" + "a" * 64 + "/data.csv",
            "sha256": "b" * 64, "sha256Source": "header-verified-object"}
    history = {"partitions": [{"tradeDate": "2026-11-26", "lineage": {"artifacts": {"sse_etf_scale": item}}}]}
    rows = ordinary_source_lineage(history, target_date="2026-11-26")
    assert rows == [{"key": item["key"], "sha256": "b" * 64, "sourceDates": ["2026-11-26"]}]
    history["partitions"].append(json.loads(json.dumps(history["partitions"][0])))
    assert ordinary_source_lineage(history, target_date="2026-11-26") == rows
    history["partitions"][1]["lineage"]["artifacts"]["sse_etf_scale"]["sha256"] = "c" * 64
    with pytest.raises(ValueError, match="hash_conflict"):
        ordinary_source_lineage(history, target_date="2026-11-26")


@pytest.mark.parametrize("day", ["2026-11-27", "2026-02-30", "not-a-date"])
def test_ordinary_lineage_rejects_invalid_or_future_partition(day):
    from scripts.resume_publish_source import ordinary_source_lineage
    with pytest.raises(ValueError, match="partition_date"):
        ordinary_source_lineage({"partitions": [{"tradeDate": day}]}, target_date="2026-11-26")


def test_ordinary_lineage_does_not_invent_missing_sources():
    from scripts.resume_publish_source import ordinary_source_lineage
    with pytest.raises(ValueError, match="lineage_missing"):
        ordinary_source_lineage({"partitions": []}, target_date="2026-11-26")


def ordinary_bundle(tmp_path):
    snapshot = {"asOf": "2026-10-16", "publication": {"status": "confirmed"}}
    history = {"partitions": [{"tradeDate": "2026-10-16", "lineage": {"artifacts": {
        "sse_etf_scale": {"status": "ready", "sha256Source": "header-verified-object",
        "sha256": "d" * 64, "key": "market-data/normalized/sse/etf_scale/2026-10-16/" + "e" * 64 + "/data.csv"}}}}]}
    files = {}
    for name, value in (("dashboard-snapshot.json", snapshot), ("stock-flow-history.json", history)):
        raw = json.dumps(value).encode()
        (tmp_path / name).write_bytes(raw)
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    plan = values()[0]
    source = plan["budgetManifest"]["publishSource"]
    source.update(evidenceContract="flow-ordinary-source-reuse-v1", sourceFileBindings=files)
    identity = {key: source[key] for key in ("sourceRunId", "artifactId", "artifactName", "artifactSha256", "artifactBytes", "producerCommit")}
    provenance = {"contractVersion": source["evidenceContract"], "artifact": identity,
                  "files": files, "targetDate": snapshot["asOf"]}
    source["sourceFingerprint"] = hashlib.sha256(json.dumps(provenance, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan.pop("planHash")
    plan["planHash"] = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return plan


def test_ordinary_evidence_is_reuse_not_collection_and_receipt_revalidates(tmp_path):
    from scripts.resume_publish_source import ordinary_source_evidence
    from scripts.create_live_recovery_receipts import _input_objects
    plan = ordinary_bundle(tmp_path)
    evidence = ordinary_source_evidence(plan, tmp_path)
    assert evidence["collectionPerformed"] is False and evidence["requestCount"] == 0
    assert evidence["collectorRunId"] == "123"
    refs = _input_objects(source_dir=tmp_path, evidence=evidence, target="2026-10-16",
                          source_fingerprint=evidence["sourceFingerprint"])
    assert len(refs) == 1 and refs[0]["sha256"] == "d" * 64
    (tmp_path / "ordinary-source-evidence.json").write_text(json.dumps(evidence))
    assert ordinary_source_evidence(plan, tmp_path) == evidence
    (tmp_path / "dashboard-snapshot.json").write_text("{}")
    with pytest.raises(ValueError, match="file_mismatch"):
        ordinary_source_evidence(plan, tmp_path)


def test_ordinary_evidence_rejects_unbound_file(tmp_path):
    from scripts.resume_publish_source import ordinary_source_evidence
    plan = ordinary_bundle(tmp_path)
    (tmp_path / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="unbound_file"):
        ordinary_source_evidence(plan, tmp_path)


def test_actual_ordinary_projection_cli_and_identity_shell(tmp_path):
    source = tmp_path / ".collection-source"
    source.mkdir()
    plan = ordinary_bundle(source)
    plan_path = tmp_path / "recovery-execution.json"
    plan_path.write_text(json.dumps(plan))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/resume_publish_source.py"),
        "--ordinary-evidence", str(plan_path), str(source)], cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src:."}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    (tmp_path / "recovery-plan.json").write_text(json.dumps({"plan": plan}))
    workflow = (ROOT / ".github/workflows/market-data-publish.yml").read_text()
    step = workflow.split("      - name: Verify source artifact identity\n", 1)[1].split("\n      - uses:", 1)[0]
    shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
    env = {**os.environ, "INPUT_RUN_KEY": plan["runKey"], "INPUT_TARGET_DATE": "2026-10-16",
           "INPUT_RECOVERY_ID": "resume-16", "INPUT_RECOVERY_DATA_DATE": "2026-10-16",
           "INPUT_RESUME_EXISTING_SOURCE": "true"}
    result = subprocess.run(["bash", "-c", shell], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    evidence_path = source / "ordinary-source-evidence.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["sourceFingerprint"] = "f" * 64
    evidence_path.write_text(json.dumps(evidence))
    result = subprocess.run(["bash", "-c", shell], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0


@pytest.mark.parametrize("change", ["fingerprint", "contract", "path"])
def test_ordinary_evidence_rejects_invalid_approved_binding(tmp_path, change):
    from scripts.resume_publish_source import ordinary_source_evidence
    plan = ordinary_bundle(tmp_path)
    source = plan["budgetManifest"]["publishSource"]
    if change == "fingerprint":
        source["sourceFingerprint"] = "f" * 64
    elif change == "contract":
        source["evidenceContract"] = "unknown"
    else:
        source["sourceFileBindings"]["../outside.json"] = {"bytes": 0, "sha256": "a" * 64}
    plan.pop("planHash")
    plan["planHash"] = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(ValueError, match="publish_resume_ordinary"):
        ordinary_source_evidence(plan, tmp_path)


def values():
    binding = dict(sourceRunId="123", artifactId=456, artifactName="immutable-source",
                   artifactBytes=189659, artifactSha256="a" * 64, producerCommit="b" * 40,
                   sourceFingerprint="c" * 64, expectedAttemptCount=4)
    plan = build_recovery_plan(campaign_id="resume", recovery_id="resume-16",
        run_key="daily-market-data:2026-10-16:evening:v1", data_dates=["2026-10-16"],
        required_stages=["dashboard", "publish"], artifact_root=".recovery/resume",
        state_dir=".recovery/resume/state", max_dates=1, max_requests=194,
        deadline="2099-01-01T00:00:00Z", budget_manifest={"publishSource": binding})
    state = dict(recoveryId="resume-16", runKey=plan["runKey"], status="AUTHORIZED",
                 expiresAt="2099-01-01T00:00:00Z", pipeline={"attemptCount": 4, "status": "FAILED_RETRYABLE"})
    artifact = dict(id=456, name="immutable-source", size_in_bytes=189659,
                    digest="sha256:" + "a" * 64, expired=False,
                    workflow_run={"id": 123, "head_sha": "b" * 40})
    return plan, state, artifact


@contextmanager
def api(plan, state, artifact, *, failure=""):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path))
            if self.headers.get("User-Agent") != "flow-publish-resume/1.0" or failure == "edge":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"error code: 1010\n")
                return
            if failure in {"private_body", "redirect"}:
                self.send_response(302 if failure == "redirect" else 401)
                self.send_header("Location", "/secret-redirect-must-not-follow")
                self.end_headers()
                self.wfile.write(b"fixture-secret response body must not be logged")
                return
            if "/actions/artifacts/" in self.path:
                assert self.headers["Authorization"] == "Bearer fixture-github"
                payload = artifact
            else:
                assert self.headers["Authorization"] == "Bearer fixture-admin"
                payload = ({"recoveryId": "resume-16", "planSha256": plan["planHash"], "plan": plan}
                           if self.path.startswith("/recovery/plan?") else state)
            self.reply(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body))
            assert self.headers.get("User-Agent") == "flow-publish-resume/1.0"
            assert body["leaseOwner"] == "github-publish:987:1"
            assert body["recoveryId"] == "resume-16"
            if self.path == "/recovery/claim":
                assert body["sourceFingerprint"] == "c" * 64
                if failure == "lost_claim":
                    self.connection.shutdown(2)
                    return
                if failure == "http_claim":
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"fixture-secret partial write details")
                    return
                result = dict(decision="skip_leased" if failure == "race" else "acquired",
                    recoveryId="resume-16", runKey=plan["runKey"], leaseOwner=body["leaseOwner"],
                    attemptCount=6 if failure == "generation" else 5,
                    leaseExpiresAt="2099-01-01T00:00:00Z")
            else:
                assert self.path == "/recovery/status"
                assert body["expectedAttemptCount"] == 5 and body["status"] == "RUNNING"
                result = dict(status="RUNNING", recoveryId="resume-16", runKey=plan["runKey"],
                              attemptCount=5, leaseOwner=body["leaseOwner"],
                              leaseExpiresAt="2099-01-01T00:00:00Z")
            self.reply(result)

        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def execute(tmp_path, plan, state, artifact, failure=""):
    output = tmp_path / "outputs"
    with api(plan, state, artifact, failure=failure) as (url, calls):
        env = {**os.environ, "PIPELINE_RECOVERY_ENABLED": "1", "INPUT_RECOVERY_ID": "resume-16",
            "INPUT_RUN_KEY": plan["runKey"], "INPUT_TARGET_DATE": "2026-10-16",
            "INPUT_RECOVERY_DATA_DATE": "2026-10-16", "INPUT_RECOVERY_STAGE": "publish",
            "INPUT_RECOVERY_PLAN_SHA256": plan["planHash"], "FLOW_PIPELINE_WINDOW_URL": url,
            "FLOW_INGEST_API_KEY": "fixture-admin", "FLOW_CF_ACCESS_CLIENT_ID": "fixture-id",
            "FLOW_CF_ACCESS_CLIENT_SECRET": "fixture-secret", "INPUT_SOURCE_RUN_ID": "123",
            "INPUT_ARTIFACT_NAME": "immutable-source", "GITHUB_REPOSITORY": "fixture/flow",
            "GITHUB_API_URL": url, "GH_TOKEN": "fixture-github", "GITHUB_RUN_ID": "987",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_OUTPUT": str(output)}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/resume_publish_source.py")],
                                env=env, capture_output=True, text=True, timeout=10)
    return result, calls, output.read_text() if output.exists() else ""


def test_publish_only_claims_pinned_artifact_without_collector(tmp_path):
    result, calls, output = execute(tmp_path, *values())
    assert result.returncode == 0, result.stderr
    assert len(calls) == 5
    assert [call[1] for call in calls if call[0] == "POST"] == ["/recovery/claim", "/recovery/status"]
    assert "lease_owner=github-publish:987:1" in output and "attempt_count=5" in output
    assert "should_run=true" in output


@pytest.mark.parametrize("fault", ["digest", "producer", "expired", "size", "name", "run", "approval", "blocked", "attempt", "hash"])
def test_invalid_source_or_approval_never_claims(tmp_path, fault):
    plan, state, artifact = values()
    if fault == "digest": artifact["digest"] = "sha256:" + "d" * 64
    if fault == "producer": artifact["workflow_run"]["head_sha"] = "d" * 40
    if fault == "expired": artifact["expired"] = True
    if fault == "size": artifact["size_in_bytes"] += 1
    if fault == "name": artifact["name"] = "other"
    if fault == "run": artifact["workflow_run"]["id"] = 999
    if fault == "approval": state["expiresAt"] = "2020-01-01T00:00:00Z"
    if fault == "blocked": state["status"] = "BLOCKED"
    if fault == "attempt": state["pipeline"]["attemptCount"] = 5
    if fault == "hash": plan["budgetManifest"]["publishSource"]["artifactBytes"] += 1
    result, calls, output = execute(tmp_path, plan, state, artifact)
    assert result.returncode != 0 and not output
    assert all(call[0] == "GET" for call in calls)
    assert "fixture-admin" not in result.stderr and "fixture-github" not in result.stderr


@pytest.mark.parametrize("failure", ["lost_claim", "generation", "race"])
def test_claim_race_or_ambiguous_write_never_retries(tmp_path, failure):
    result, calls, output = execute(tmp_path, *values(), failure=failure)
    assert sum(call[1] == "/recovery/claim" for call in calls) == 1
    assert not any(call[1] == "/recovery/status" for call in calls)
    if failure == "race":
        assert result.returncode == 0 and "should_run=false" in output
    else:
        assert result.returncode != 0 and not output


def test_workflow_entry_is_opt_in_and_passes_new_generation():
    text = (ROOT / ".github/workflows/market-data-publish.yml").read_text()
    assert 'default: false\n        type: boolean' in text
    assert text.index("python3 scripts/resume_publish_source.py") < text.index("actions/setup-python")
    assert "INPUT_LEASE_OWNER: ${{ needs.publish_window_gate.outputs.lease_owner }}" in text
    assert "INPUT_RECOVERY_ATTEMPT_COUNT: ${{ needs.publish_window_gate.outputs.attempt_count || '' }}" in text
    assert "market-data-collect.yml/dispatches" not in text


@pytest.mark.parametrize("completed", [True, False])
def test_completed_or_valid_lease_skips_before_any_write(tmp_path, completed):
    plan, state, artifact = values()
    if completed:
        state["status"] = "COMPLETED"
    else:
        state["status"] = "ACTIVE"
        state["pipeline"].update(status="RUNNING", leaseExpiresAt="2099-01-01T00:00:00Z")
    result, calls, output = execute(tmp_path, plan, state, artifact)
    assert result.returncode == 0 and "should_run=false" in output
    assert all(call[0] == "GET" for call in calls)


@pytest.mark.parametrize("mode,fingerprint,expected", [
    ("false", "other", 0), ("true", "c" * 64, 0), ("true", "other", 1),
])
def test_actual_source_identity_shell_keeps_normal_compatibility(tmp_path, mode, fingerprint, expected):
    text = (ROOT / ".github/workflows/market-data-publish.yml").read_text()
    step = text.split("      - name: Verify source artifact identity\n", 1)[1].split("\n      - uses:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    source = tmp_path / ".collection-source"
    source.mkdir()
    (source / "dashboard-snapshot.json").write_text(json.dumps({
        "asOf": "2026-10-16", "publication": {"status": "confirmed"}}))
    (source / "recovery-evidence.json").write_text(json.dumps({"sourceFingerprint": fingerprint}))
    plan = values()[0]
    (tmp_path / "recovery-plan.json").write_text(json.dumps({"plan": plan}))
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "INPUT_RUN_KEY": plan["runKey"], "INPUT_TARGET_DATE": "2026-10-16",
             "INPUT_RECOVERY_ID": "resume-16", "INPUT_RECOVERY_DATA_DATE": "2026-10-16",
             "INPUT_RESUME_EXISTING_SOURCE": mode})
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("failure,code", [
    ("edge", "publish_resume_http_read_status_403_edge_1010"),
    ("private_body", "publish_resume_http_read_status_401"),
    ("redirect", "publish_resume_redirect_refused"),
    ("http_claim", "publish_resume_http_claim_503_write_outcome_unknown"),
])
def test_safe_stage_diagnostics_keep_auth_redirect_and_unknown_guards(tmp_path, failure, code):
    result, calls, output = execute(tmp_path, *values(), failure=failure)
    assert result.returncode != 0 and not output
    assert code in result.stderr
    assert "fixture-secret" not in result.stderr and "secret-redirect" not in result.stderr
    assert not any(call[1] == "/recovery/status" for call in calls)
    assert sum(call[1] == "/recovery/claim" for call in calls) == (1 if failure == "http_claim" else 0)
    assert len(calls) == (4 if failure == "http_claim" else 1)
