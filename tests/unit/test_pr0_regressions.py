from __future__ import annotations

import importlib
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.dashboard import build_dashboard_snapshot
from trading_desk.read_models import build_read_models


def _write(frame: pd.DataFrame, path: Path) -> str:
    frame.to_csv(path, index=False)
    return path.name


def _complete_inputs(tmp_path: Path, dates: list[str]) -> tuple[Path, Path]:
    etf_dates = []
    for index, trade_date in enumerate(dates):
        etf_dates.extend(
            [
                {
                    "trade_date": trade_date,
                    "instrument_code": "510300",
                    "instrument_name": "沪深300ETF",
                    "shares": 100_000_000 + index,
                    "nav": 1.0,
                    "investment_type": "股票基金",
                },
            ]
        )
    szse_etf = [
        {
            "trade_date": trade_date,
            "instrument_code": "159919",
            "instrument_name": "沪深300ETF",
            "shares": 100_000_000 + (index * 100_000_000),
            "nav": 1.0,
            "investment_type": "股票基金",
        }
        for index, trade_date in enumerate(dates)
    ]
    margin = [
        {
            "trade_date": trade_date,
            "financing_balance_yuan": 1_000_000_000 + index,
            "financing_buy_yuan": 10_000_000,
            "securities_lending_balance_yuan": 2_000_000,
            "securities_lending_sell_volume_shares": 1_000,
            "margin_total_balance_yuan": 1_002_000_000,
        }
        for index, trade_date in enumerate(dates)
    ]
    connect = [
        {
            "trade_date": trade_date,
            "market": market,
            "direction": direction,
            "total_turnover": 1_000_000_000,
            "net_buy": 100_000_000 if direction == "southbound" else None,
        }
        for trade_date in dates
        for market in ("SSE", "SZSE")
        for direction in ("southbound", "northbound")
    ]
    paths = {
        "sse_etf": _write(pd.DataFrame(etf_dates), tmp_path / "sse_etf.csv"),
        "szse_etf": _write(pd.DataFrame(szse_etf), tmp_path / "szse_etf.csv"),
        "sse_margin": _write(pd.DataFrame(margin), tmp_path / "sse_margin.csv"),
        "szse_margin": _write(pd.DataFrame(margin), tmp_path / "szse_margin.csv"),
        "stock_connect_summary": _write(pd.DataFrame(connect), tmp_path / "connect.csv"),
    }
    manifest_path = tmp_path / "manifest.json"
    quality_path = tmp_path / "quality.json"
    manifest_path.write_text(
        json.dumps(
            {
                "requested_dates": dates,
                "finished_at": "2026-08-14T03:17:42+00:00",
                "combined_paths": paths,
            }
        ),
        encoding="utf-8",
    )
    quality_path.write_text(
        json.dumps({"counts": {"pass": 10, "warn": 0, "fail": 0}, "phase0_gate_status": "pass"}),
        encoding="utf-8",
    )
    return manifest_path, quality_path


def test_homepage_etf_summary_includes_szse_flow(tmp_path: Path) -> None:
    manifest_path, quality_path = _complete_inputs(tmp_path, ["2026-08-12", "2026-08-13"])
    snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=tmp_path,
    )

    assert snapshot["metrics"][0]["value"] == "+1.00"
    assert "159919" in {row["code"] for row in snapshot["etfFlows"]["inflow"]}


def test_market_history_read_model_is_not_truncated_to_recent_60_points(tmp_path: Path) -> None:
    dates = pd.bdate_range("2026-05-18", periods=61).strftime("%Y-%m-%d").tolist()
    manifest_path, quality_path = _complete_inputs(tmp_path, dates)
    snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=tmp_path,
    )

    history = {
        "schemaVersion": 1,
        "asOf": dates[-1],
        "rows": [
            {
                "trade_date": date,
                "margin_balance": 20_000.0 + index,
                "margin_available": 1,
                "southbound_net": float(index),
                "northbound_turnover": 1_000.0,
                "connect_available": 1,
                "source_snapshot_sha256": f"{index:064x}",
                "observed_at": f"{date}T09:00:00Z",
            }
            for index, date in enumerate(dates)
        ],
    }
    catalog = {
        "schemaVersion": 1,
        "asOf": dates[-1],
        "basis": {"shareDate": dates[-1], "pcfDate": dates[-1]},
        "coverage": {"stockCount": 0, "matchedEtfCount": 0},
        "items": [],
    }
    payloads, manifest = build_read_models(
        snapshot,
        require_materialized=True,
        stock_catalog=catalog,
        market_history=history,
    )

    assert len(payloads["margin"]["data"]["marginSeries"]) == len(dates)
    assert len(payloads["connect"]["data"]["connectSeries"]) == len(dates)
    assert manifest["marketHistory"]["margin"]["points"] == len(dates)
    assert manifest["marketHistory"]["connect"]["points"] == len(dates)


@pytest.mark.xfail(strict=True, reason="PR3 must block incompatible pressure history instead of silently bootstrapping")
def test_incompatible_previous_pressure_release_is_not_silent_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("scripts.build_stock_pressure_state")

    class FakeResponse:
        status_code = 200
        headers = {"x-flow-release-id": "release:2026-08-12:0123456789abcdef"}

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, **_: object) -> FakeResponse:
        if url.endswith("previous-candidates"):
            return FakeResponse(
                {
                    "candidates": [
                        {
                            "releaseId": "release:2026-08-12:0123456789abcdef",
                            "asOf": "2026-08-12",
                        }
                    ]
                }
            )
        return FakeResponse({"asOf": "2026-08-12", "data": {"pressure": {"schemaVersion": 1}}})

    monkeypatch.setattr(module.requests, "get", fake_get)
    _, continuity = module._fetch_previous_items(
        "https://flow.example",
        timeout=1.0,
        before_date="2026-08-13",
    )

    assert continuity["historyStatus"] == "incompatible_previous_release"
