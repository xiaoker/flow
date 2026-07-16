from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from trading_desk.dashboard import _classify_etf, build_dashboard_snapshot


def _write(frame: pd.DataFrame, path: Path) -> str:
    frame.to_csv(path, index=False)
    return path.name


def test_build_dashboard_snapshot_from_normalized_frames(tmp_path: Path) -> None:
    dates = ["2026-07-10", "2026-07-13", "2026-07-14"]
    etf_rows = []
    for trade_date, first, second in zip(dates, [100, 110, 125], [200, 190, 180]):
        etf_rows.extend(
            [
                {"trade_date": trade_date, "instrument_code": "000001", "instrument_name": "ETF A", "shares": first},
                {"trade_date": trade_date, "instrument_code": "000002", "instrument_name": "ETF B", "shares": second},
            ]
        )
    margin_rows = [
        {"trade_date": trade_date, "financing_balance_yuan": value}
        for trade_date, value in zip(dates, [1000, 1100, 1200])
    ]
    connect_rows = []
    for trade_date in dates:
        for market in ("SSE", "SZSE"):
            connect_rows.extend(
                [
                    {"trade_date": trade_date, "market": market, "direction": "southbound", "total_turnover": 1000, "net_buy": 100},
                    {"trade_date": trade_date, "market": market, "direction": "northbound", "total_turnover": 2000, "net_buy": None},
                ]
            )

    combined_paths = {
        "sse_etf": _write(pd.DataFrame(etf_rows), tmp_path / "sse_etf.csv"),
        "szse_etf": _write(pd.DataFrame([{"trade_date": "2026-07-14", "instrument_code": "159001", "instrument_name": "货币ETF", "shares": 300, "nav": 100.0, "investment_type": "货币市场基金"}]), tmp_path / "szse_etf.csv"),
        "sse_margin": _write(pd.DataFrame(margin_rows), tmp_path / "sse_margin.csv"),
        "szse_margin": _write(pd.DataFrame(margin_rows), tmp_path / "szse_margin.csv"),
        "stock_connect_summary": _write(pd.DataFrame(connect_rows), tmp_path / "connect.csv"),
    }
    manifest = {
        "requested_dates": dates,
        "finished_at": "2026-07-14T23:59:00+08:00",
        "combined_paths": combined_paths,
    }
    quality = {
        "counts": {"pass": 10, "warn": 1, "fail": 0},
        "phase0_gate_status": "conditional_pass",
    }
    manifest_path = tmp_path / "manifest.json"
    quality_path = tmp_path / "quality.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    quality_path.write_text(json.dumps(quality), encoding="utf-8")

    snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=tmp_path,
    )

    assert snapshot["asOf"] == "2026-07-14"
    assert snapshot["quality"] == {"pass": 10, "warn": 1, "fail": 0}
    assert snapshot["metrics"][0]["value"] == "+0.00"
    assert snapshot["metrics"][1]["value"] == "+0.00"
    assert snapshot["etfFlows"]["inflow"][0]["code"] == "000001"
    assert snapshot["etfTerminal"]["availablePeriods"] == ["1d", "3d"]
    assert snapshot["etfTerminal"]["dailySeries"][-1]["coverage"] == 2
    assert snapshot["etfTerminal"]["instruments"][0]["periods"]["3d"] is not None
    szse_item = next(item for item in snapshot["etfTerminal"]["instruments"] if item["market"] == "SZSE")
    assert szse_item["category"] == "债券货币"
    assert szse_item["periods"] == {"1d": None, "3d": None}
    assert snapshot["etfTerminal"]["universe"]["marketCounts"] == {"SSE": 2, "SZSE": 1}
    assert snapshot["marginSeries"][-1]["change"] == 0.0
    assert snapshot["marginSeries"][-1]["sseBalance"] == 0.0
    assert len(snapshot["connectSeries"]) == 3
    assert snapshot["dataStatus"][1]["status"] == "warning"


def test_etf_classification_prefers_broad_index_before_theme_keywords() -> None:
    assert _classify_etf("科创50ETF") == "宽基"
    assert _classify_etf("科创半导体ETF") == "科技"
    assert _classify_etf("深市现金ETF", "货币市场基金") == "债券货币"
