from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_desk.etf_flow_state import (
    build_etf_flow_state,
    materialize_dashboard_snapshot,
    materialize_etf_terminal,
)


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


def test_materialization_hides_instruments_absent_from_latest_official_day() -> None:
    state = {
        "version": 1,
        "classificationVersion": "2026-07-v3",
        "dates": ["2026-08-06", "2026-08-07"],
        "instruments": [
            {
                "code": "510300",
                "name": "沪深300ETF",
                "market": "SSE",
                "category": "宽基",
                "values": [1.0, 2.0],
                "cashValues": [1.0, 2.0],
            },
            {
                "code": "512280",
                "name": " stale ETF ",
                "market": "SSE",
                "category": "科技",
                "values": [3.0, None],
                "cashValues": [3.0, None],
            },
        ],
    }

    terminal = materialize_etf_terminal(state)

    assert [item["code"] for item in terminal["instruments"]] == ["510300"]
    assert terminal["dailySeries"][0]["coverage"] == 2
    assert terminal["dailySeries"][1]["coverage"] == 1


def test_dashboard_materialization_rederives_headline_from_both_markets() -> None:
    state = {
        "version": 1,
        "dates": ["2026-08-06", "2026-08-07"],
        "instruments": [
            {"code": "510300", "name": "沪深300ETF", "market": "SSE", "category": "宽基", "values": [0.0, 1.0]},
            {"code": "159919", "name": "沪深300ETF", "market": "SZSE", "category": "宽基", "values": [0.0, 2.0]},
        ],
    }
    snapshot = {
        "asOf": "2026-08-07",
        "metrics": [
            {
                "id": "etf",
                "eyebrow": "上交所 ETF 份额变化",
                "value": "+0.00",
                "delta": "0 只增加 · 0 只减少",
                "tone": "neutral",
                "basis": {"detail": "旧口径"},
            }
        ],
        "etfFlows": {"inflow": [], "outflow": []},
        "etfTerminal": materialize_etf_terminal(state),
        "dataStatus": [],
    }

    materialized = materialize_dashboard_snapshot(snapshot, state)

    assert materialized["metrics"][0]["value"] == "+3.00"
    assert materialized["metrics"][0]["eyebrow"] == "沪深 ETF 份额变化"
    assert {row["code"] for row in materialized["etfFlows"]["inflow"]} == {"159919", "510300"}
