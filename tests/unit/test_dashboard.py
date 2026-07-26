from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from trading_desk.dashboard import (
    _build_etf_terminal_data,
    _classify_etf,
    build_dashboard_snapshot,
)
from trading_desk.etf_classification import (
    CLASSIFICATION_VERSION,
    classify_etf,
)


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
    assert snapshot["metrics"][0]["basis"]["kind"] == "calculated"
    assert snapshot["metrics"][2]["basis"]["kind"] == "official"
    assert snapshot["etfFlows"]["inflow"][0]["code"] == "000001"
    assert snapshot["etfTerminal"]["availablePeriods"] == ["1d"]
    assert snapshot["etfTerminal"]["dailySeries"][-1]["coverage"] == 2
    assert "3d" not in snapshot["etfTerminal"]["instruments"][0]["periods"]
    szse_item = next(item for item in snapshot["etfTerminal"]["instruments"] if item["market"] == "SZSE")
    assert szse_item["category"] == "货币"
    assert szse_item["groupType"] == "money"
    assert szse_item["classificationConfidence"] == "high"
    assert szse_item["periods"] == {"1d": None}
    assert snapshot["etfTerminal"]["universe"]["marketCounts"] == {"SSE": 2, "SZSE": 1}
    assert snapshot["etfTerminal"]["classificationVersion"] == CLASSIFICATION_VERSION
    assert snapshot["marginSeries"][-1]["change"] == 0.0
    assert snapshot["marginSeries"][-1]["sseBalance"] == 0.0
    assert len(snapshot["connectSeries"]) == 3
    assert snapshot["dataStatus"][1]["status"] == "ready"
    assert snapshot["dataStatus"][1]["basis"] == "official"
    assert "列表页规模日期" in snapshot["dataStatus"][1]["detail"]


def test_etf_classification_prefers_broad_index_before_theme_keywords() -> None:
    assert _classify_etf("科创50ETF") == "宽基"
    assert _classify_etf("科创半导体ETF") == "科技"
    assert _classify_etf("深市现金ETF", "货币市场基金") == "货币"


def test_etf_classification_blocks_known_cross_category_pollution() -> None:
    cases = {
        "科创债券": "债券",
        "科创医药": "医药",
        "科创生物": "医药",
        "科创板50": "宽基",
        "科创100": "宽基",
        "科创200": "宽基",
        "港股通信息技术ETF": "港股海外",
        "中韩芯片ETF": "港股海外",
        "科创AI": "人工智能机器人",
        "科创机械": "新能源制造",
    }
    for name, expected in cases.items():
        assert classify_etf(name=name).category == expected


def test_etf_classification_excludes_non_etf_products_and_unmatched_groups() -> None:
    lof = classify_etf(
        name="人工智能LOF",
        code="161631",
        market="SZSE",
        etf_type="LOF",
        investment_type="股票基金",
    )
    assert lof.eligible is False
    assert lof.exclusion_reason == "非ETF产品"

    unmatched = classify_etf(
        name="治理ETF",
        code="510010",
        market="SSE",
        etf_type="单市",
    )
    assert unmatched.category == "其他"
    assert unmatched.include_in_group is False
    assert unmatched.confidence == "low"


def test_etf_group_aggregation_keeps_share_and_cash_estimates_separate() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-07-20",
                "instrument_code": "512480",
                "instrument_name": "半导体ETF",
                "shares": 100_000_000,
                "nav": 2.0,
                "etf_type": "跨市",
                "investment_type": None,
            },
            {
                "trade_date": "2026-07-21",
                "instrument_code": "512480",
                "instrument_name": "半导体ETF",
                "shares": 120_000_000,
                "nav": 2.0,
                "etf_type": "跨市",
                "investment_type": None,
            },
            {
                "trade_date": "2026-07-20",
                "instrument_code": "588080",
                "instrument_name": "科创板50",
                "shares": 200_000_000,
                "nav": None,
                "etf_type": "单市",
                "investment_type": None,
            },
            {
                "trade_date": "2026-07-21",
                "instrument_code": "588080",
                "instrument_name": "科创板50",
                "shares": 190_000_000,
                "nav": None,
                "etf_type": "单市",
                "investment_type": None,
            },
        ]
    )
    terminal = _build_etf_terminal_data(
        frame,
        pd.DataFrame(
            [
                {
                    "trade_date": "2026-07-21",
                    "instrument_code": "159995",
                    "instrument_name": "芯片ETF",
                    "shares": 80_000_000,
                    "nav": 1.2,
                    "etf_type": "ETF",
                    "investment_type": "股票基金",
                }
            ]
        ),
        ["2026-07-20", "2026-07-21"],
    )
    groups = {item["name"]: item for item in terminal["groupPeriods"]}

    assert groups["科技"]["periods"]["1d"] == 0.2
    assert groups["科技"]["estimatedCashPeriods"]["1d"] == 0.4
    assert groups["科技"]["componentCount"] == 2
    assert groups["科技"]["comparableCount"]["1d"] == 1
    assert groups["宽基"]["periods"]["1d"] == -0.1
    assert groups["宽基"]["estimatedCashPeriods"]["1d"] is None
