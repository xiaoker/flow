from __future__ import annotations

import json

import pytest

from scripts import recover_szse_etf_history as history
from scripts.recover_szse_etf_history import RequestBudget


def test_request_budget_counts_each_top_level_request_once() -> None:
    budget = RequestBudget(maximum=2, min_interval_seconds=0)

    budget.before_request()
    budget.before_request()

    assert budget.request_count == 2
    with pytest.raises(RuntimeError, match="recovery_request_budget_exceeded"):
        budget.before_request()
    assert budget.request_count == 2


def test_request_budget_paces_concurrent_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("scripts.recover_szse_etf_history.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("scripts.recover_szse_etf_history.time.sleep", sleep)
    budget = RequestBudget(maximum=3, min_interval_seconds=2)

    budget.before_request()
    budget.before_request()
    budget.before_request()

    assert sleeps == [2.0, 2.0]
    assert budget.request_count == 3


@pytest.mark.parametrize(
    ("maximum", "interval"),
    [(0, 0), (-1, 0), (1, -0.1), (1, float("nan"))],
)
def test_request_budget_rejects_invalid_bounds(maximum: int, interval: float) -> None:
    with pytest.raises(ValueError):
        RequestBudget(maximum=maximum, min_interval_seconds=interval)


def test_szse_history_request_falls_back_to_second_official_host(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return history.subprocess.CompletedProcess(command, 22, stdout="", stderr="")
        return history.subprocess.CompletedProcess(command, 0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(history.subprocess, "run", run)
    budget = RequestBudget(maximum=2, min_interval_seconds=0)
    payload = history._request_json(
        history._session(), {"SHOWTYPE": "JSON", "txtDm": "159915"}, budget=budget
    )

    assert payload == []
    assert [call[0][-1] for call in calls] == [
        history.WWW_REPORT_URL,
        history.REPORT_URL,
    ]
    assert budget.active_host == "fund.szse.cn"
    assert budget.request_count == 2


def test_szse_history_request_rejects_invalid_configured_host(monkeypatch) -> None:
    monkeypatch.setenv(history.SZSE_HOST_ENV, "evil.example")
    budget = RequestBudget(maximum=2, min_interval_seconds=0)
    with pytest.raises(ValueError, match="invalid_szse_host"):
        budget.host_order()
    assert budget.request_count == 0
