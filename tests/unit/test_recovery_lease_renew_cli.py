"""Recovery and ordinary pipeline heartbeat contracts."""
import json
import io
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from scripts import recovery_lease_renew as renew


ROOT = Path(__file__).resolve().parents[2]
COLLECT_WORKFLOW = ROOT / ".github/workflows/market-data-collect.yml"
PUBLISH_WORKFLOW = ROOT / ".github/workflows/market-data-publish.yml"


def run_clock(
    monkeypatch,
    tmp_path,
    *,
    mode="recovery",
    stop_at=None,
    deadline_seconds=3600,
    body=None,
    status_code=200,
    cancel_status=202,
    github_run_id="123456",
    renewal_interval=180,
    calls_out=None,
):
    seconds = [0.0]
    calls = []
    epoch = datetime(2026, 10, 29, tzinfo=timezone.utc)
    stop = tmp_path / "stop"
    evidence = tmp_path / "evidence.json"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(seconds=seconds[0])

    renewal_body = body or {
        "status": "RUNNING",
        "recoveryStatus": "ACTIVE",
        "attemptCount": 4,
        "leaseExpiresAt": "2026-10-29T01:00:00Z",
        "rowsRead": 7,
        "rowsWritten": 6,
    }

    def post(url, payload, headers, timeout=30):
        calls.append({"at": seconds[0], "url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if url.endswith("/cancel"):
            return cancel_status, {}
        return status_code, renewal_body

    def sleep(duration):
        assert 0 < duration <= 5
        seconds[0] += duration
        if stop_at is not None and seconds[0] >= stop_at:
            stop.touch()

    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-not-a-credential")
    monkeypatch.setenv("GITHUB_REPOSITORY", "xiaoker/flow")
    monkeypatch.setenv("GITHUB_RUN_ID", github_run_id)
    monkeypatch.setenv("PIPELINE_LEASE_HEARTBEAT_GITHUB_TOKEN", "fixture-github-token")
    monkeypatch.setattr(renew, "datetime", Clock)
    monkeypatch.setattr(renew.time, "monotonic", lambda: seconds[0])
    monkeypatch.setattr(renew.time, "sleep", sleep)
    monkeypatch.setattr(renew, "_post_json", post)
    argv = [
        "renew", "--url", "https://offline.invalid/status", "--run-key", "daily-market-data:2026-10-29:evening:v1",
        "--lease-owner", "owner", "--attempt", "4", "--interval-seconds", str(renewal_interval),
        "--stop-file", str(stop), "--evidence", str(evidence),
        "--deadline", (epoch + timedelta(seconds=deadline_seconds)).isoformat(),
    ]
    if mode == "recovery":
        argv.extend(["--recovery-id", "fixture"])
    else:
        argv.extend(["--mode", "ordinary"])
    monkeypatch.setattr(sys, "argv", argv)
    try:
        renew.main()
    finally:
        if calls_out is not None:
            calls_out.extend(calls)
    return calls, seconds[0], json.loads(evidence.read_text())


def test_recovery_stop_polls_do_not_issue_early_renewals(monkeypatch, tmp_path):
    calls, elapsed, evidence = run_clock(monkeypatch, tmp_path, stop_at=185)
    assert [call["at"] for call in calls] == [0, 180]
    assert all(call["payload"]["recoveryId"] == "fixture" for call in calls)
    assert elapsed == 185 and evidence["renewalCount"] == 2
    assert evidence["status"] == "STOPPED"
    assert evidence["rowsRead"] == 7 and evidence["rowsWritten"] == 6


def test_stop_marker_is_observed_within_five_seconds(monkeypatch, tmp_path):
    calls, elapsed, evidence = run_clock(monkeypatch, tmp_path, stop_at=7)
    assert [call["at"] for call in calls] == [0]
    assert 7 <= elapsed <= 12
    assert evidence["status"] == "STOPPED"


def test_deadline_stops_recovery_without_an_early_or_expired_renewal(monkeypatch, tmp_path):
    calls, elapsed, evidence = run_clock(monkeypatch, tmp_path, deadline_seconds=12)
    assert [call["at"] for call in calls] == [0]
    assert elapsed == 12
    assert evidence["status"] == "DEADLINE"
    assert evidence["errorCode"] == "live_recovery_deadline_expired"


def test_ordinary_heartbeat_renews_same_attempt_without_recovery_identity(monkeypatch, tmp_path):
    response = {
        "status": "RUNNING",
        "runKey": "daily-market-data:2026-10-29:evening:v1",
        "leaseOwner": "owner",
        "recoveryId": None,
        "decision": "renewed",
        "attemptCount": 4,
        "leaseExpiresAt": "2026-10-29T00:15:00Z",
        "rowsWritten": 1,
    }
    calls, elapsed, evidence = run_clock(
        monkeypatch, tmp_path, mode="ordinary", stop_at=305, body=response, renewal_interval=300
    )
    assert [call["at"] for call in calls] == [0, 300]
    assert all(call["url"] == "https://offline.invalid/status" for call in calls)
    assert all(call["payload"] == {
        "runKey": "daily-market-data:2026-10-29:evening:v1",
        "leaseOwner": "owner",
        "expectedAttemptCount": 4,
        "status": "RUNNING",
        "renewLease": True,
    } for call in calls)
    assert elapsed == 305
    assert evidence["status"] == "STOPPED"
    assert evidence["renewalCount"] == 2 and evidence["rowsWritten"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"leaseOwner": "other"},
        {"attemptCount": 5},
        {"leaseExpiresAt": "2026-10-28T23:59:00Z"},
        {"rowsWritten": 0},
        {"rowsWritten": True},
        {"rowsWritten": 9_007_199_254_740_992},
    ],
)
def test_ordinary_identity_or_receipt_failure_cancels_current_workflow(monkeypatch, tmp_path, capsys, change):
    response = {
        "status": "RUNNING",
        "runKey": "daily-market-data:2026-10-29:evening:v1",
        "leaseOwner": "owner",
        "recoveryId": None,
        "decision": "renewed",
        "attemptCount": 4,
        "leaseExpiresAt": "2026-10-29T00:15:00Z",
        "rowsWritten": 1,
    }
    response.update(change)
    calls = []
    with pytest.raises(SystemExit) as error:
        run_clock(monkeypatch, tmp_path, mode="ordinary", body=response, calls_out=calls)
    assert error.value.code == 2
    assert len(calls) == 2
    assert calls[0]["url"].endswith("/status")
    assert calls[1]["url"] == "https://api.github.com/repos/xiaoker/flow/actions/runs/123456/cancel"
    assert calls[1]["payload"] is None
    assert calls[1]["headers"]["Authorization"] == "Bearer fixture-github-token"
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["status"] == "FAILED"
    assert evidence["renewalRequestCount"] == 1
    assert evidence["unknownRenewalCount"] == 1
    assert evidence["workflowCancellation"] == "requested"
    assert evidence["workflowCancellationLastHttpStatus"] == 202
    assert "fixture-github-token" not in capsys.readouterr().err


def test_ordinary_http_conflict_fails_closed_and_cancels_run(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as error:
        run_clock(monkeypatch, tmp_path, mode="ordinary", status_code=409)
    assert error.value.code == 2
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["httpStatus"] == 409
    assert evidence["renewalRequestCount"] == 1
    assert evidence["unknownRenewalCount"] == 1
    assert evidence["workflowCancellation"] == "requested"


def test_cancel_conflict_is_not_reported_as_a_successful_cancellation(monkeypatch, tmp_path):
    calls = []
    with pytest.raises(SystemExit) as error:
        run_clock(monkeypatch, tmp_path, mode="ordinary", status_code=409, cancel_status=409, calls_out=calls)
    assert error.value.code == 2
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["workflowCancellation"] == "failed"
    assert evidence["workflowCancellationLastHttpStatus"] == 409
    assert sum(call["url"].endswith("/cancel") for call in calls) == 3


def test_invalid_github_run_identity_is_recorded_without_masking_renewal_failure(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as error:
        run_clock(monkeypatch, tmp_path, mode="ordinary", status_code=409, github_run_id="not-a-run-id")
    assert error.value.code == 2
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["errorCode"] == "pipeline_window_lease_renew_failed"
    assert evidence["workflowCancellation"] == "failed"
    assert evidence["workflowCancellationError"] == "invalid_run_identity"


def test_ordinary_heartbeat_accepts_physical_index_write_count(monkeypatch, tmp_path):
    response = {
        "status": "RUNNING",
        "runKey": "daily-market-data:2026-10-29:evening:v1",
        "leaseOwner": "owner",
        "recoveryId": None,
        "decision": "renewed",
        "attemptCount": 4,
        "leaseExpiresAt": "2026-10-29T00:15:00Z",
        "rowsWritten": 3,
    }
    _calls, _elapsed, evidence = run_clock(
        monkeypatch, tmp_path, mode="ordinary", stop_at=5, body=response
    )
    assert evidence["status"] == "STOPPED"
    assert evidence["renewalCount"] == 1 and evidence["rowsWritten"] == 3


def test_recovery_http_error_preserves_legacy_error_code(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as error:
        run_clock(monkeypatch, tmp_path, status_code=409)
    assert error.value.code == 2
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["errorCode"] == "live_recovery_lease_renew_failed"
    assert evidence["httpStatus"] == 409


def test_post_json_uses_bounded_stdlib_http_and_preserves_request_contract(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == 64 * 1024 + 1
            return b'{"status":"RUNNING"}'

    def open_request(request, *, timeout):
        assert isinstance(request, Request)
        assert request.full_url == "https://flow.invalid/status"
        assert request.method == "POST"
        assert request.data == b'{"runKey":"fixture"}'
        assert request.get_header("Content-type") == "application/json"
        assert request.get_header("Authorization") == "Bearer fixture"
        assert request.get_header("User-agent") == "flow-lease-heartbeat/1.0"
        assert request.get_header("Accept") == "application/json"
        assert timeout == 7
        return Response()

    monkeypatch.setattr(renew, "urlopen", open_request)
    status, body = renew._post_json(
        "https://flow.invalid/status",
        {"runKey": "fixture"},
        {"Authorization": "Bearer fixture", "Content-Type": "application/json"},
        timeout=7,
    )
    assert status == 200 and body == {"status": "RUNNING"}


def test_post_json_maps_http_error_without_reading_or_returning_body(monkeypatch):
    def reject_request(*_args, **_kwargs):
        raise HTTPError(
            "https://flow.invalid/status", 409, "Conflict", {},
            io.BytesIO(b"do-not-expose-this-response-body"),
        )

    monkeypatch.setattr(renew, "urlopen", reject_request)
    status, body = renew._post_json(
        "https://flow.invalid/status", {"runKey": "fixture"}, {}, timeout=1
    )
    assert status == 409 and body == {}


def test_http_error_diagnostic_only_retains_safe_response_metadata(monkeypatch):
    headers = {
        "CF-Ray": "0123456789abcdef-SJC",
        "Content-Type": "text/html; charset=UTF-8",
        "cf-mitigated": "challenge",
        "Location": "https://secret.invalid/token",
        "Set-Cookie": "secret=credential",
    }

    def reject(*_args, **_kwargs):
        raise HTTPError("https://flow.invalid/status", 403, "Forbidden", headers,
                        io.BytesIO(b"secret-response-body"))

    monkeypatch.setattr(renew, "urlopen", reject)
    status, body = renew._post_json("https://flow.invalid/status", {}, {})
    assert status == 403
    assert body == {"httpDiagnostic": {
        "cfRay": "0123456789abcdef-SJC",
        "contentType": "text/html; charset=UTF-8",
        "mitigation": "challenge",
    }}
    assert "secret" not in json.dumps(body)


def test_http_error_diagnostic_rejects_arbitrary_header_values(monkeypatch):
    def reject(*_args, **_kwargs):
        raise HTTPError("https://flow.invalid/status", 403, "Forbidden",
                        {"CF-Ray": "secret", "Content-Type": "secret", "cf-mitigated": "secret"},
                        io.BytesIO(b"secret"))

    monkeypatch.setattr(renew, "urlopen", reject)
    assert renew._post_json("https://flow.invalid/status", {}, {}) == (403, {})


def test_post_json_rejects_oversized_control_response(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            assert size == 64 * 1024 + 1
            return b"x" * size

    monkeypatch.setattr(renew, "urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="size limit"):
        renew._post_json("https://flow.invalid/status", None, {}, timeout=1)


def test_collect_and_publish_workflows_start_and_stop_ordinary_heartbeat():
    collect = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    collect_start = collect.index("Start ordinary pipeline-window lease heartbeat")
    collect_confirm = collect.index("Confirm ordinary pipeline-window lease heartbeat")
    collect_collect = collect.index("Collect and validate official data")
    collect_stop = collect.index("Stop ordinary pipeline-window lease heartbeat")
    collect_wait = collect.index("wait: lease_heartbeat")
    collect_terminal = collect.index("Mark pipeline window terminal status")
    publish_start = publish.index("Start ordinary pipeline-window lease heartbeat")
    publish_confirm = publish.index("Confirm ordinary publish lease heartbeat")
    publish_download = publish.index("Download immutable collection source bundle")
    publish_stop = publish.index("Stop ordinary pipeline-window lease heartbeat")
    publish_wait = publish.index("wait: lease_heartbeat")
    publish_terminal = publish.index("Mark pipeline window terminal status")
    for workflow in (collect, publish):
        assert "--mode ordinary" in workflow
        assert "--interval-seconds 300" in workflow
        assert "background: true" in workflow
        assert "wait: lease_heartbeat" in workflow
        assert "nohup" not in workflow
        assert "PIPELINE_LEASE_HEARTBEAT_GITHUB_TOKEN" in workflow
        assert "actions: write" in workflow
        assert "pipeline-window-lease-heartbeat.json" in workflow
        assert "pipeline-window-lease-heartbeat.stop" in workflow
    assert collect_start < collect_confirm < collect_collect < collect_stop < collect_wait < collect_terminal
    assert publish_start < publish_confirm < publish_download < publish_stop < publish_wait < publish_terminal
    assert "expectedAttemptCount" in collect and "attempt_count" in collect
    assert "attempt_count" in publish
    assert "steps.lease_heartbeat_wait.outcome == 'success'" in collect
    assert "steps.lease_heartbeat_wait.outcome == 'success'" in publish


def _publisher_heartbeat_shell(tmp_path, *, gate="true", recovery="", confirm=False, attempt="2"):
    """Execute the real YAML shell with its job-scoped flag, without network IO.

    The python3 stub records startup arguments and emits a local healthy
    receipt. It tests shell wiring only; renewal HTTP/identity behavior is
    covered by the existing renew tests above, not claimed as production.
    """
    workflow = PUBLISH_WORKFLOW.read_text()
    job = workflow.split("\n  publish:\n", 1)[1]
    environment = job.split("    env:\n", 1)[1].split("    steps:\n", 1)[0]
    env = {k: v for k, v in os.environ.items() if k != "PIPELINE_WINDOW_GATE_ENABLED"}
    binding = re.search(r"(?m)^      PIPELINE_WINDOW_GATE_ENABLED: (.+)$", environment)
    if binding:
        assert binding[1] == "${{ needs.publish_window_gate.outputs.gate_enabled }}"
        env["PIPELINE_WINDOW_GATE_ENABLED"] = gate
    env.update({"INPUT_RECOVERY_ID": recovery, "INPUT_PIPELINE_ATTEMPT_COUNT": attempt,
        "INPUT_RUN_KEY": "daily-market-data:2026-11-27:reconcile:v1", "INPUT_LEASE_OWNER": "fixture-owner",
        "FLOW_PIPELINE_WINDOW_URL": "https://offline.invalid/pipeline-window"})
    name = "Confirm ordinary publish lease heartbeat" if confirm else "Start ordinary pipeline-window lease heartbeat"
    step = job.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
    receipt = json.dumps({"status": "RUNNING", "renewalCount": 1, "runKey": env["INPUT_RUN_KEY"],
                         "leaseOwner": env["INPUT_LEASE_OWNER"], "attemptCount": 2})
    fixture = (
        "date() { printf '%s\\n' '2099-01-01T00:00:00Z'; };\n"
        "sleep() { :; };\n"
        "python3() { printf '%s\\n' \"$@\" > invocation.txt; "
        + "printf '%s\\n' '" + receipt + "' > pipeline-window-lease-heartbeat.json; };\n"
    )
    return subprocess.run(["bash", "-c", fixture + shell], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=10)


def test_publish_job_verified_flag_reaches_start_and_confirm_shells(tmp_path):
    start = _publisher_heartbeat_shell(tmp_path)
    assert start.returncode == 0, start.stderr
    args = (tmp_path / "invocation.txt").read_text().splitlines()
    assert args[:3] == ["scripts/recovery_lease_renew.py", "--mode", "ordinary"]
    assert args[args.index("--attempt") + 1] == "2"
    assert args[args.index("--interval-seconds") + 1] == "300"
    confirm = _publisher_heartbeat_shell(tmp_path, confirm=True)
    assert confirm.returncode == 0, confirm.stderr
    assert "heartbeat started" in confirm.stdout


def test_missing_job_binding_reproduces_production_unbound_variable(tmp_path, monkeypatch):
    legacy = tmp_path / "missing-job-binding.yml"
    legacy.write_text(PUBLISH_WORKFLOW.read_text().replace(
        "      PIPELINE_WINDOW_GATE_ENABLED: ${{ needs.publish_window_gate.outputs.gate_enabled }}\n", "", 1))
    monkeypatch.setattr(sys.modules[__name__], "PUBLISH_WORKFLOW", legacy)
    result = _publisher_heartbeat_shell(tmp_path)
    assert result.returncode != 0
    assert "PIPELINE_WINDOW_GATE_ENABLED: unbound variable" in result.stderr
    assert not (tmp_path / "invocation.txt").exists()


@pytest.mark.parametrize("gate,recovery", [("false", ""), ("true", "fixture-recovery")])
def test_publish_disabled_or_recovery_mode_skips_ordinary_heartbeat(tmp_path, gate, recovery):
    for confirm in (False, True):
        result = _publisher_heartbeat_shell(tmp_path, gate=gate, recovery=recovery, confirm=confirm)
        assert result.returncode == 0, result.stderr
    assert not (tmp_path / "invocation.txt").exists()


@pytest.mark.parametrize("gate", ["", "1", "unknown"])
@pytest.mark.parametrize("confirm", [False, True])
def test_publish_unknown_gate_does_not_silently_disable_heartbeat(tmp_path, gate, confirm):
    result = _publisher_heartbeat_shell(tmp_path, gate=gate, confirm=confirm)
    assert result.returncode != 0
    assert "missing or invalid" in result.stderr
    assert not (tmp_path / "invocation.txt").exists()


def test_publish_enabled_heartbeat_still_requires_attempt_and_matching_receipt(tmp_path):
    result = _publisher_heartbeat_shell(tmp_path, attempt="0")
    assert result.returncode != 0 and "valid ordinary attempt" in result.stderr
    assert not (tmp_path / "invocation.txt").exists()
    assert _publisher_heartbeat_shell(tmp_path).returncode == 0
    receipt = tmp_path / "pipeline-window-lease-heartbeat.json"
    value = json.loads(receipt.read_text())
    value["leaseOwner"] = "wrong-owner"
    receipt.write_text(json.dumps(value))
    result = _publisher_heartbeat_shell(tmp_path, confirm=True)
    assert result.returncode != 0 and "was not confirmed" in result.stderr
