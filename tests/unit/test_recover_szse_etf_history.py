from __future__ import annotations

import pytest

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
