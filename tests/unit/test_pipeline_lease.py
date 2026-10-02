from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from trading_desk.pipeline_adapters import BoundedOfficialSession
from trading_desk.pipeline_application import build_application_identity, canonical_sha256
from trading_desk.pipeline_lease import OrdinaryPipelineLease
from trading_desk.pipeline_runner import PipelineExecutionContext, PipelineRunnerError, dependency_lock_digest, run_pipeline_request


ROOT = Path(__file__).resolve().parents[2]
RUN_KEY = "daily-market-data:2026-09-30:reconcile:v1"
START = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)


class Clock:
    seconds = 0.0

    def now(self):
        return START + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds


def context():
    return PipelineExecutionContext(
        identity=build_application_identity(run_key=RUN_KEY, window_date="2026-09-30", target_date="2026-09-29", producer_build_sha="a" * 40),
        mode="ordinary", lease_owner="runner:fixture", lease_expires_at="2026-09-30T09:15:00Z",
        authorization_kind="existing-admin", recovery_id=None, recovery_plan_sha256=None,
        dependency_lock_sha256="b" * 64, lease_attempt_count=1,
    )


def body(clock):
    return {"runKey": RUN_KEY, "windowDate": "2026-09-30", "targetDate": "2026-09-29",
            "leaseOwner": "runner:fixture", "attemptCount": 1, "status": "DISPATCHED", "recoveryId": None,
            "leaseExpiresAt": (clock.now() + timedelta(minutes=15)).isoformat(),
            "decision": "renewed", "rowsWritten": 3}


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.closed = payload, status, False

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode()

    def close(self):
        self.closed = True


class Session:
    def __init__(self, clock):
        self.clock, self.calls, self.override, self.status, self.responses = clock, [], {}, 200, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = Response({**body(self.clock), **self.override}, self.status)
        self.responses.append(response)
        return response


def guard(clock, session=None, **kwargs):
    return OrdinaryPipelineLease(base_url="https://flow.xiaoker.com", ingest_key="fixture-admin-secret",
                                 access_client_id="fixture-id", access_client_secret="fixture-access-secret",
                                 expected_attempt_count=1, session=session or Session(clock), now=clock.now,
                                 monotonic=clock.monotonic, **kwargs)


def test_real_authority_checked_and_heartbeat_not_per_boundary_poll():
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    assert lease.verify(context())
    for _ in range(500):
        lease.check()
    assert len(session.calls) == 1
    clock.seconds = 179
    lease.check()
    assert len(session.calls) == 1
    clock.seconds = 180
    lease.check()
    assert [call[0] for call in session.calls] == ["GET", "POST"]
    assert session.calls[-1][2]["json"]["expectedAttemptCount"] == 1
    assert session.calls[-1][2]["allow_redirects"] is False
    assert session.calls[-1][2]["timeout"] == (5, 10)
    assert all(response.closed for response in session.responses)
    assert lease.evidence()["rowsWritten"] == 3
    assert lease.evidence()["rowsRead"]["basis"] == "unknown"
    assert "secret" not in json.dumps(lease.evidence())


@pytest.mark.parametrize("override", [
    {"runKey": "daily-market-data:2026-10-01:reconcile:v1"}, {"leaseOwner": "runner:other-owner"},
    {"attemptCount": 2}, {"attemptCount": True}, {"targetDate": "2026-09-30"},
    {"status": "COMPLETED"}, {"status": "FAILED_RETRYABLE"}, {"recoveryId": "recovery-1"},
    {"leaseExpiresAt": "2026-09-30T08:59:00Z"}, {"leaseExpiresAt": "2026-09-30T09:15:00"},
])
def test_stale_owner_attempt_date_terminal_recovery_and_expired_proofs_fail_closed(override):
    clock = Clock()
    session = Session(clock)
    session.override = override
    lease = guard(clock, session)
    with pytest.raises(PipelineRunnerError):
        lease.verify(context())
    count = len(session.calls)
    with pytest.raises(PipelineRunnerError):
        lease.check()
    assert len(session.calls) == count


def test_expired_lease_never_sends_takeover_or_renewal():
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    lease.verify(context())
    clock.seconds = 901
    with pytest.raises(PipelineRunnerError, match="pipeline_lease_expired"):
        lease.check()
    assert len(session.calls) == 1


def test_receipt_confirmation_rechecks_authority_even_before_heartbeat_due():
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    lease.verify(context())
    session.override = {"attemptCount": 2}
    with pytest.raises(PipelineRunnerError, match="identity_conflict"):
        lease.confirm()
    assert [call[0] for call in session.calls] == ["GET", "GET"]


def test_concurrent_boundary_checks_share_one_due_renewal():
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    lease.verify(context())
    clock.seconds = 180
    threads = [threading.Thread(target=lease.check) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert lease.renewal_count == 1
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [302, 401, 403, 409, 429, 503])
def test_renewal_http_error_is_sticky_without_retries_or_secret_echo(status):
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    lease.verify(context())
    clock.seconds = 180
    session.status = status
    with pytest.raises(PipelineRunnerError, match="pipeline_lease_http_rejected") as error:
        lease.check()
    assert "secret" not in str(error.value)
    with pytest.raises(PipelineRunnerError):
        lease.check()
    assert len(session.calls) == 2


def test_request_and_elapsed_budgets_stop_without_an_extra_request():
    clock = Clock()
    lease = guard(clock, maximum_requests=1)
    lease.verify(context())
    clock.seconds = 180
    with pytest.raises(PipelineRunnerError, match="request_budget_exceeded"):
        lease.check()
    clock = Clock()
    lease = guard(clock, maximum_seconds=100)
    lease.verify(context())
    clock.seconds = 100
    with pytest.raises(PipelineRunnerError, match="execution_deadline"):
        lease.check()


def test_response_byte_limit_and_timeout_fail_closed_without_echoing_secrets():
    clock = Clock()
    session = Session(clock)
    session.override = {"padding": "x" * 64001}
    lease = guard(clock, session)
    with pytest.raises(PipelineRunnerError, match="response_too_large"):
        lease.verify(context())
    assert session.responses[0].closed
    class TimeoutSession:
        def request(self, *_args, **_kwargs):
            raise RuntimeError("fixture-admin-secret")
    lease = guard(clock, TimeoutSession())
    with pytest.raises(PipelineRunnerError, match="transport_or_payload_failed") as error:
        lease.verify(context())
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("override", [{"decision": "claimed"}, {"rowsWritten": 0}, {"rowsWritten": True}, {"rowsWritten": 33}])
def test_ambiguous_renewal_receipt_retains_unknown_write_outcome(override):
    clock = Clock()
    session = Session(clock)
    lease = guard(clock, session)
    lease.verify(context())
    clock.seconds = 180
    session.override = override
    with pytest.raises(PipelineRunnerError, match="renewal_receipt_invalid"):
        lease.check()
    assert lease.evidence()["unconfirmedWriteOutcome"] is True
    assert lease.renewal_count == 0


def test_source_boundary_after_pacing_cannot_issue_request_under_lost_lease():
    class Source:
        headers = {}
        calls = 0
        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response({})
    clock = Clock()
    control = Session(clock)
    lease = guard(clock, control)
    lease.verify(context())
    source = Source()
    session = BoundedOfficialSession(maximum=10, min_interval_seconds=2, session=source,
                                    monotonic=clock.monotonic, sleep=lambda seconds: setattr(clock, "seconds", clock.seconds + seconds),
                                    lease_check=lease.check)
    session.get("https://example.org/fixture")
    clock.seconds = 180
    control.override = {"leaseOwner": "runner:new-owner"}
    with pytest.raises(PipelineRunnerError, match="identity_conflict"):
        session.get("https://example.org/fixture")
    assert source.calls == 1
    assert session.request_count == 1


@pytest.mark.parametrize("lease_lost", [True, False])
def test_runner_checks_completion_authority_and_reports_control_writes(tmp_path, lease_lost):
    clock = Clock()
    control = Session(clock)
    lease = guard(clock, control)
    config = {"fixture": "bounded"}
    lock = ROOT / "web/package-lock.json"
    payload = {"identity": context().identity.as_dict(), "mode": "ordinary",
               "lease": {"owner": "runner:fixture", "status": "ACTIVE", "expiresAt": "2026-09-30T09:15:00Z", "attemptCount": 1},
               "authorization": {"kind": "existing-admin", "verified": True},
               "dependencyLockSha256": dependency_lock_digest([lock], root=ROOT)["sha256"],
               "stages": ["core_collection"], "stageInputs": {"core_collection": canonical_sha256(config)},
               "stageConfigs": {"core_collection": config}}
    class Adapter:
        allowed_side_effects = ("filesystem:isolated_stage_workspace",)
        def verify_checkpoint(self, **_kwargs):
            return True
        def run(self, **_kwargs):
            clock.seconds = 180
            control.status = 409 if lease_lost else 200
            return {"status": "COMPLETED", "outputSha256": "c" * 64,
                    "allowedSideEffects": list(self.allowed_side_effects), "evidence": {}}
    def run():
        return run_pipeline_request(payload, state_dir=tmp_path / "state", output_path=tmp_path / "result.json",
                             producer_build_sha="a" * 40, dependency_lock_paths=[lock], project_root=ROOT, now=clock.now(),
                             stage_adapters={"core_collection": Adapter()}, lease_guard=lease)
    if lease_lost:
        with pytest.raises(PipelineRunnerError, match="pipeline_lease_http_rejected"):
            run()
        assert not (tmp_path / "state/checkpoints.json").exists()
        assert not (tmp_path / "result.json").exists()
    else:
        report = run()
        assert report["status"] == "COMPLETED"
        assert report["sideEffects"]["remoteWrites"] is True
        assert report["sideEffects"]["workflowDispatch"] is False
        assert report["sideEffects"]["activeReleaseChanged"] is False
        assert report["leaseEvidence"]["rowsWritten"] == 3
        assert [call[0] for call in control.calls] == ["GET", "POST", "GET", "GET"]


def test_actual_http_transport_uses_existing_auth_and_exact_route():
    clock = Clock()
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            captured.append((self.command, self.path, self.headers.get("Authorization")))
            encoded = json.dumps(body(clock)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert value["runKey"] == RUN_KEY and value["expectedAttemptCount"] == 1 and value["renewLease"] is True
            self.do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        lease = OrdinaryPipelineLease(base_url=f"http://127.0.0.1:{server.server_port}", ingest_key="fixture",
                                      access_client_id="fixture-id", access_client_secret="fixture-secret",
                                      expected_attempt_count=1, now=clock.now, monotonic=clock.monotonic, allow_loopback_fixture=True)
        lease.verify(context())
        clock.seconds = 180
        lease.check()
        assert [call[0] for call in captured] == ["GET", "POST"]
        assert all(call[2] == "Bearer fixture" for call in captured)
        assert captured[0][1].startswith("/api/v1/admin/pipeline-window/status?runKey=")
        assert captured[1][1] == "/api/v1/admin/pipeline-window/status"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
