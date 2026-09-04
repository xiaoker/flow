from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_live_recovery import (
    BudgetedRateLimitedSession,
    LiveRecoveryError,
    _post_child_request_reserve,
    _fetch_market_history,
    run_live_recovery,
)
from trading_desk.recovery_execution import build_recovery_plan


def _plan(dates: list[str]) -> dict:
    return build_recovery_plan(
        campaign_id="recovery-20260904",
        recovery_id="recovery-20260904-live",
        run_key="daily-market-data:2026-08-28:reconcile:v1",
        data_dates=dates,
        required_stages=["dashboard", "publish"],
        artifact_root=".recovery/recovery-20260904-live",
        state_dir=".recovery/recovery-20260904-live/state",
        max_dates=3,
        max_requests=100,
        deadline="2026-12-31T00:00:00.000Z",
        budget_manifest={"source": "test", "measured": True},
    )


def test_live_runner_fails_closed_instead_of_silently_using_last_date(tmp_path: Path) -> None:
    with pytest.raises(LiveRecoveryError, match="live_recovery_multi_date_requires_date_major_dispatch") as caught:
        run_live_recovery(
            plan=_plan(["2026-08-27", "2026-08-28"]),
            expected_plan_hash=None,
            attempt=1,
            producer_build_sha="a" * 64,
            workspace_root=tmp_path,
            base_url="https://flow.example",
        )
    assert caught.value.status == "BLOCKED_QUALITY"


class _Transport:
    def __init__(self) -> None:
        self.headers = {}
        self.request_timeout = (1.0, 2.0)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs: object):
        self.calls.append((method, url))
        return object()


class _StatusTransport(_Transport):
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    def request(self, method: str, url: str, **_kwargs: object):
        self.calls.append((method, url))
        return type("Response", (), {"status_code": self.status_code})()


def test_budgeted_session_counts_get_and_post_and_enforces_budget() -> None:
    transport = _Transport()
    session = BudgetedRateLimitedSession(transport, maximum=2, min_interval_seconds=0)
    session.get("https://source.example/a")
    session.post("https://source.example/b")
    assert session.request_count == 2
    assert transport.calls == [("GET", "https://source.example/a"), ("POST", "https://source.example/b")]
    with pytest.raises(LiveRecoveryError, match="live_recovery_request_budget_exceeded"):
        session.get("https://source.example/c")


@pytest.mark.parametrize("status_code", [403, 429])
def test_budgeted_session_fails_closed_on_source_rejection(status_code: int) -> None:
    transport = _StatusTransport(status_code)
    session = BudgetedRateLimitedSession(transport, maximum=2, min_interval_seconds=0)
    with pytest.raises(LiveRecoveryError, match="live_recovery_source_unavailable"):
        session.get("https://source.example/rejected")
    assert session.request_count == 1
    assert len(transport.calls) == 1


def test_post_child_reserve_covers_history_reads_and_retry_attempts() -> None:
    assert _post_child_request_reserve({"artifacts": [{"releaseId": "a"}, {"releaseId": "b"}]}) == 11
    with pytest.raises(LiveRecoveryError, match="live_recovery_history_artifacts_missing"):
        _post_child_request_reserve({"artifacts": []})


class _JsonResponse:
    def __init__(self, value: dict) -> None:
        self._value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._value


class _JsonSession:
    def __init__(self, values: list[dict]) -> None:
        self.values = iter(values)

    def get(self, *_args: object, **_kwargs: object) -> _JsonResponse:
        return _JsonResponse(next(self.values))


def test_market_history_must_contain_target_date(tmp_path: Path) -> None:
    session = _JsonSession(
        [
            {"rows": [{"trade_date": "2026-08-27"}]},
            {"releaseId": "release-before"},
        ]
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_market_history_target_missing"):
        _fetch_market_history(
            base_url="https://flow.example",
            target="2026-08-28",
            output_dir=tmp_path,
            headers={"CF-Access-Client-Id": "id"},
            session=session,
        )
