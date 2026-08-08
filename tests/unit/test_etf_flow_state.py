from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk.etf_flow_state import build_etf_flow_state


def _snapshot(
    path: Path,
    date: str,
    value: float,
    *,
    name: str = "芯片ETF",
    cash: float | None = None,
) -> tuple[Path, dict]:
    payload = {
        "asOf": date,
        "etfTerminal": {
            "instruments": [
                {
                    "code": "512760",
                    "name": name,
                    "market": "SSE",
                    "assetClass": "股票",
                    "periods": {"1d": value},
                    "estimatedCashPeriods": {"1d": cash},
                }
            ]
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload


def test_builds_compact_versioned_daily_flow_state(tmp_path: Path) -> None:
    snapshots = [
        _snapshot(tmp_path / "2026-07-21.json", "2026-07-21", 1.2),
        _snapshot(tmp_path / "2026-07-22.json", "2026-07-22", -0.4),
    ]

    state = build_etf_flow_state(snapshots)

    assert state["dates"] == ["2026-07-21", "2026-07-22"]
    assert state["classificationVersion"] == "2026-07-v3"
    assert state["calculationMethod"] == "sum_of_daily_official_share_changes"
    assert state["cashCalculationMethod"] == "sum_of_daily_share_changes_times_same_day_nav"
    assert state["quality"]["status"] == "pass"
    assert state["instruments"][0]["category"] == "科技"
    assert state["instruments"][0]["values"] == [1.2, -0.4]
    assert len(state["sourceSnapshots"][0]["sha256"]) == 64


def test_carries_daily_cash_values_without_revaluing_old_share_changes(tmp_path: Path) -> None:
    snapshots = [
        _snapshot(tmp_path / "2026-07-21.json", "2026-07-21", 1.2, cash=2.4),
        _snapshot(tmp_path / "2026-07-22.json", "2026-07-22", -0.4, cash=-0.8),
    ]

    state = build_etf_flow_state(snapshots)

    assert state["instruments"][0]["cashValues"] == [2.4, -0.8]


def test_rejects_duplicate_snapshot_dates(tmp_path: Path) -> None:
    snapshots = [
        _snapshot(tmp_path / "one.json", "2026-07-21", 1.2),
        _snapshot(tmp_path / "two.json", "2026-07-21", 2.4),
    ]

    with pytest.raises(ValueError, match="Duplicate snapshot date"):
        build_etf_flow_state(snapshots)


def test_keeps_only_requested_rolling_window(tmp_path: Path) -> None:
    snapshots = [
        _snapshot(tmp_path / f"{day}.json", f"2026-07-{day:02d}", float(day))
        for day in range(1, 5)
    ]

    state = build_etf_flow_state(snapshots, max_trading_days=3)

    assert state["dates"] == ["2026-07-02", "2026-07-03", "2026-07-04"]
    assert state["instruments"][0]["values"] == [2.0, 3.0, 4.0]
