"""The legacy recovery heartbeat must poll locally, not renew every poll."""
import json
import sys
from datetime import datetime, timedelta, timezone

from scripts import recovery_lease_renew as renew


def run_clock(monkeypatch, tmp_path, *, stop_at=None, deadline_seconds=3600):
    seconds = [0.0]
    posts = []
    epoch = datetime(2026, 10, 29, tzinfo=timezone.utc)
    stop = tmp_path / "stop"
    evidence = tmp_path / "evidence.json"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(seconds=seconds[0])

    class Response:
        ok = True
        def json(self):
            return {"status": "RUNNING", "recoveryStatus": "ACTIVE", "attemptCount": 4,
                    "leaseExpiresAt": "2026-10-29T01:00:00Z", "rowsRead": 7, "rowsWritten": 6}

    class Session:
        def post(self, *_args, **kwargs):
            posts.append(seconds[0])
            assert kwargs["json"]["renewLease"] is True
            return Response()

    def sleep(duration):
        assert 0 < duration <= 5
        seconds[0] += duration
        if stop_at is not None and seconds[0] >= stop_at:
            stop.touch()

    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-not-a-credential")
    monkeypatch.setattr(renew, "datetime", Clock)
    monkeypatch.setattr(renew.time, "monotonic", lambda: seconds[0])
    monkeypatch.setattr(renew.time, "sleep", sleep)
    monkeypatch.setattr(renew.requests, "Session", Session)
    monkeypatch.setattr(sys, "argv", ["renew", "--url", "https://offline.invalid/status",
        "--recovery-id", "fixture", "--run-key", "fixture", "--lease-owner", "owner",
        "--attempt", "4", "--interval-seconds", "180", "--stop-file", str(stop),
        "--evidence", str(evidence), "--deadline", (epoch + timedelta(seconds=deadline_seconds)).isoformat()])
    renew.main()
    return posts, seconds[0], json.loads(evidence.read_text())


def test_stop_polls_do_not_issue_early_renewals(monkeypatch, tmp_path):
    posts, elapsed, evidence = run_clock(monkeypatch, tmp_path, stop_at=185)
    assert posts == [0, 180]
    assert elapsed == 185 and evidence["renewalCount"] == 2
    assert evidence["status"] == "STOPPED"


def test_stop_marker_is_observed_within_five_seconds(monkeypatch, tmp_path):
    posts, elapsed, evidence = run_clock(monkeypatch, tmp_path, stop_at=7)
    assert posts == [0]
    assert 7 <= elapsed <= 12
    assert evidence["status"] == "STOPPED"


def test_deadline_stops_without_an_early_or_expired_renewal(monkeypatch, tmp_path):
    posts, elapsed, evidence = run_clock(monkeypatch, tmp_path, deadline_seconds=12)
    assert posts == [0]
    assert elapsed == 12
    assert evidence["status"] == "DEADLINE"
