from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .adapters import OfficialMarketAdapters
from .http import build_session
from .storage import new_run_id


def discover_recent_trading_dates(*, end_date: date, count: int = 3) -> list[str]:
    if count < 2:
        raise ValueError("count must be at least 2")
    start_date = end_date - timedelta(days=max(21, count * 5))
    with tempfile.TemporaryDirectory(prefix="flow-date-discovery-") as directory:
        adapter = OfficialMarketAdapters(
            session=build_session(),
            data_root=Path(directory),
            run_id=new_run_id(),
        )
        frame, _ = adapter.fetch_sse_margin(start_date.isoformat(), end_date.isoformat())
    available = sorted(frame["trade_date"].dropna().astype(str).unique().tolist())
    if len(available) < count:
        raise RuntimeError(f"only discovered {len(available)} trading dates")
    return available[-count:]


def _read_csv(workspace_root: Path, relative_path: str) -> pd.DataFrame:
    return pd.read_csv(workspace_root / relative_path, dtype={"instrument_code": str})


def _signed(value: float, decimals: int = 2) -> str:
    return f"{value:+,.{decimals}f}"


def _latest_etf_flows(frame: pd.DataFrame, dates: list[str]) -> tuple[dict[str, Any], Any]:
    latest_date, previous_date = dates[-1], dates[-2]
    latest = frame.loc[
        frame["trade_date"].astype(str).eq(latest_date),
        ["instrument_code", "instrument_name", "shares"],
    ].rename(columns={"shares": "latest_shares"})
    previous = frame.loc[
        frame["trade_date"].astype(str).eq(previous_date),
        ["instrument_code", "shares"],
    ].rename(columns={"shares": "previous_shares"})
    changes = latest.merge(previous, on="instrument_code", how="inner")
    changes["delta"] = changes["latest_shares"] - changes["previous_shares"]

    def rows(ascending: bool) -> list[dict[str, Any]]:
        ordered = changes.sort_values("delta", ascending=ascending).head(5)
        return [
            {
                "code": str(row.instrument_code).zfill(6),
                "name": str(row.instrument_name),
                "value": round(float(row.delta) / 100_000_000, 2),
            }
            for row in ordered.itertuples()
        ]

    summary = {
        "net": float(changes["delta"].sum()) / 100_000_000,
        "increasers": int(changes["delta"].gt(0).sum()),
        "decreasers": int(changes["delta"].lt(0).sum()),
        "latest_count": int(latest["instrument_code"].nunique()),
    }
    return {"inflow": rows(False), "outflow": rows(True)}, summary


def build_dashboard_snapshot(
    *, manifest_path: Path, quality_path: Path, workspace_root: Path
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    paths = manifest["combined_paths"]
    dates = sorted(manifest["requested_dates"])
    as_of = dates[-1]

    sse_etf = _read_csv(workspace_root, paths["sse_etf"])
    szse_etf = _read_csv(workspace_root, paths["szse_etf"])
    sse_margin = _read_csv(workspace_root, paths["sse_margin"])
    szse_margin = _read_csv(workspace_root, paths["szse_margin"])
    connect = _read_csv(workspace_root, paths["stock_connect_summary"])

    etf_flows, etf_summary = _latest_etf_flows(sse_etf, dates)
    margin = pd.concat([sse_margin, szse_margin], ignore_index=True)
    margin_daily = (
        margin.groupby("trade_date", as_index=False)
        .agg(financing_balance_yuan=("financing_balance_yuan", "sum"))
        .sort_values("trade_date")
    )
    latest_margin = float(margin_daily.iloc[-1]["financing_balance_yuan"])
    previous_margin = float(margin_daily.iloc[-2]["financing_balance_yuan"])
    three_day_start = float(margin_daily.iloc[0]["financing_balance_yuan"])

    connect_latest = connect[connect["trade_date"].astype(str).eq(as_of)].copy()
    southbound = connect_latest[connect_latest["direction"].eq("southbound")]
    northbound = connect_latest[connect_latest["direction"].eq("northbound")]
    southbound_net = float(southbound["net_buy"].sum()) / 100_000_000
    northbound_turnover = float(northbound["total_turnover"].sum()) / 100_000_000

    market_labels = {
        ("SSE", "southbound"): "沪港通南向",
        ("SZSE", "southbound"): "深港通南向",
        ("SSE", "northbound"): "沪股通北向",
        ("SZSE", "northbound"): "深股通北向",
    }
    connect_rows = []
    for direction in ("southbound", "northbound"):
        for market in ("SSE", "SZSE"):
            row = connect_latest[
                connect_latest["direction"].eq(direction)
                & connect_latest["market"].eq(market)
            ].iloc[0]
            if direction == "southbound":
                value = float(row["net_buy"]) / 100_000_000
                connect_rows.append(
                    {
                        "name": market_labels[(market, direction)],
                        "value": _signed(value),
                        "unit": "亿港元",
                        "description": f"成交 {float(row['total_turnover']) / 100_000_000:,.2f} 亿港元",
                        "tone": "positive" if value >= 0 else "negative",
                    }
                )
            else:
                connect_rows.append(
                    {
                        "name": market_labels[(market, direction)],
                        "value": f"{float(row['total_turnover']) / 100_000_000:,.2f}",
                        "unit": "亿元",
                        "description": "仅官方成交额",
                        "tone": "neutral",
                    }
                )

    margin_series = [
        {
            "date": str(row.trade_date)[5:],
            "balance": round(float(row.financing_balance_yuan) / 100_000_000, 2),
            "label": f"{float(row.financing_balance_yuan) / 1_000_000_000_000:.4f} 万亿",
        }
        for row in margin_daily.itertuples()
    ]
    counts = quality["counts"]
    szse_date = str(szse_etf["trade_date"].max())
    south_market_values = {
        row["name"]: row["value"] for row in connect_rows if "南向" in row["name"]
    }
    north_rows = [row for row in connect_rows if "北向" in row["name"]]

    return {
        "asOf": as_of,
        "observedAt": manifest["finished_at"],
        "mode": "GitHub Actions 官方数据快照",
        "gate": quality["phase0_gate_status"],
        "quality": {"pass": counts["pass"], "warn": counts["warn"], "fail": counts["fail"]},
        "metrics": [
            {
                "id": "etf",
                "eyebrow": "上交所 ETF 份额净变化",
                "value": _signed(etf_summary["net"]),
                "unit": "亿份",
                "delta": f"{etf_summary['increasers']} 只增加 · {etf_summary['decreasers']} 只减少",
                "tone": "positive" if etf_summary["net"] >= 0 else "negative",
                "note": "份额口径，非净申购金额",
            },
            {
                "id": "southbound",
                "eyebrow": "南向净买入",
                "value": _signed(southbound_net),
                "unit": "亿港元",
                "delta": f"沪港通 {south_market_values['沪港通南向']} · 深港通 {south_market_values['深港通南向']}",
                "tone": "positive" if southbound_net >= 0 else "negative",
                "note": "官方买入额 − 卖出额",
            },
            {
                "id": "margin",
                "eyebrow": "沪深融资余额",
                "value": f"{latest_margin / 1_000_000_000_000:.4f}",
                "unit": "万亿元",
                "delta": f"较前日 {_signed((latest_margin - previous_margin) / 100_000_000)} 亿元",
                "tone": "positive" if latest_margin >= previous_margin else "negative",
                "note": "沪深两市合计",
            },
            {
                "id": "northbound",
                "eyebrow": "北向成交总额",
                "value": f"{northbound_turnover:,.2f}",
                "unit": "亿元",
                "delta": f"沪股通 {north_rows[0]['value']} · 深股通 {north_rows[1]['value']}",
                "tone": "neutral",
                "note": "当前官方不披露北向净流入",
            },
        ],
        "etfFlows": etf_flows,
        "marginSeries": margin_series,
        "connect": connect_rows,
        "dataStatus": [
            {"name": "上交所 ETF", "status": "ready", "asOf": as_of[5:], "detail": f"{etf_summary['latest_count']} 只·3 日"},
            {"name": "深交所 ETF", "status": "warning", "asOf": szse_date[5:], "detail": "当前快照"},
            {"name": "融资融券", "status": "ready", "asOf": as_of[5:], "detail": "沪深完整"},
            {"name": "互联互通", "status": "ready", "asOf": as_of[5:], "detail": "4 通道"},
        ],
    }
