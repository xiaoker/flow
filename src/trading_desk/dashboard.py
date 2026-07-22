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


def _etf_dimensions(name: str, investment_type: str | None = None) -> tuple[str, str, str]:
    normalized = name.upper()
    official_type = "" if pd.isna(investment_type) else str(investment_type)
    if "货币" in official_type or any(keyword in normalized for keyword in ("货币", "现金", "快线")):
        return "债券货币", "货币", "official_investment_type" if official_type else "name_keyword"
    if "债券" in official_type or any(keyword in normalized for keyword in ("国债", "政金债", "信用债", "可转债", "短融", "同业存单")):
        return "债券货币", "债券", "official_investment_type" if official_type else "name_keyword"
    if any(keyword in normalized for keyword in ("沪深300", "中证500", "中证1000", "中证2000", "中证A500", "上证50", "科创50", "创业板", "深证100", "A50", "上证180")):
        return "宽基", "股票", "name_keyword"
    rules = (
        ("科技", ("半导", "芯片", "通信", "5G", "软件", "计算机", "人工智能", "AI", "机器人", "科创")),
        ("医药", ("医药", "医疗", "创新药", "生物", "疫苗")),
        ("港股海外", ("恒生", "港股", "中概", "纳指", "标普", "日经", "德国", "法国", "海外")),
        ("商品", ("黄金", "有色", "煤炭", "钢铁", "化工", "豆粕", "能源", "商品")),
        ("红利价值", ("红利", "价值", "低波", "央企")),
        ("金融地产", ("银行", "证券", "券商", "金融", "地产")),
        ("新能源制造", ("新能源", "光伏", "电池", "汽车", "军工", "机械", "制造")),
        ("宽基", ("300", "500", "1000", "2000", "A50", "上证", "创业板", "中证", "沪深")),
    )
    for category, keywords in rules:
        if any(keyword in normalized for keyword in keywords):
            asset_class = "跨境" if category == "港股海外" else "商品" if category == "商品" else "股票"
            return category, asset_class, "name_keyword"
    asset_class = "股票" if "股票" in official_type else "其他"
    return "其他", asset_class, "official_investment_type" if official_type else "name_keyword"


def _classify_etf(name: str, investment_type: str | None = None) -> str:
    return _etf_dimensions(name, investment_type)[0]


def _build_etf_terminal_data(
    frame: pd.DataFrame, szse_frame: pd.DataFrame, dates: list[str]
) -> dict[str, Any]:
    available_dates = [
        trade_date
        for trade_date in dates
        if frame["trade_date"].astype(str).eq(trade_date).any()
    ]
    if len(available_dates) < 2:
        return {"dailySeries": [], "instruments": [], "groupPeriods": []}

    selected = frame.loc[
        frame["trade_date"].astype(str).isin(available_dates),
    ].reindex(
        columns=[
            "trade_date",
            "instrument_code",
            "instrument_name",
            "shares",
            "nav",
            "etf_type",
            "investment_type",
        ]
    ).copy()
    selected["trade_date"] = selected["trade_date"].astype(str)
    selected["instrument_code"] = selected["instrument_code"].astype(str).str.zfill(6)
    selected["shares"] = pd.to_numeric(selected["shares"], errors="coerce")

    metadata = (
        selected.sort_values("trade_date")
        .drop_duplicates("instrument_code", keep="last")
        .set_index("instrument_code")
    )
    pivot = selected.pivot_table(
        index="instrument_code", columns="trade_date", values="shares", aggfunc="last"
    )
    latest_date = available_dates[-1]
    lookbacks = {
        "1d": 1,
        "3d": 3,
        "1w": 5,
        "2w": 10,
        "1m": 21,
        "3m": 63,
        "6m": 126,
        "12m": 252,
    }
    anchors: dict[str, str] = {
        period: available_dates[-offset - 1]
        for period, offset in lookbacks.items()
        if len(available_dates) > offset
    }
    current_year = latest_date[:4]
    prior_year_dates = [date for date in available_dates if date[:4] < current_year]
    if prior_year_dates:
        anchors["ytd"] = prior_year_dates[-1]
    available_periods = [
        period
        for period in ("1d", "3d", "1w", "2w", "1m", "3m", "6m", "ytd", "12m")
        if period in anchors
    ]

    instruments = []
    for code in pivot.index:
        row_metadata = metadata.loc[code]
        name = str(row_metadata.get("instrument_name", code))
        investment_type = row_metadata.get("investment_type")
        category, asset_class, classification_source = _etf_dimensions(name, investment_type)
        latest_shares = pivot.at[code, latest_date] if latest_date in pivot.columns else None
        period_values: dict[str, float | None] = {}
        for period in available_periods:
            anchor = anchors[period]
            current_value = pivot.at[code, latest_date]
            anchor_value = pivot.at[code, anchor]
            period_values[period] = (
                None
                if pd.isna(current_value) or pd.isna(anchor_value)
                else round(float(current_value - anchor_value) / 100_000_000, 2)
            )
        instruments.append(
            {
                "code": str(code),
                "name": name,
                "market": "SSE",
                "category": category,
                "assetClass": asset_class,
                "classificationSource": classification_source,
                "shares": None if pd.isna(latest_shares) else round(float(latest_shares) / 100_000_000, 2),
                "nav": None if pd.isna(row_metadata.get("nav")) else round(float(row_metadata.get("nav")), 4),
                "periods": period_values,
                "coverage": int(pivot.loc[code, available_dates].notna().sum()),
            }
        )

    if not szse_frame.empty and "instrument_code" in szse_frame.columns:
        szse_selected = szse_frame.reindex(
            columns=[
                "trade_date",
                "instrument_code",
                "instrument_name",
                "shares",
                "nav",
                "etf_type",
                "investment_type",
            ]
        ).dropna(subset=["instrument_code"])
        if not szse_selected.empty:
            szse_selected["instrument_code"] = szse_selected["instrument_code"].astype(str).str.zfill(6)
            szse_latest_date = szse_selected["trade_date"].astype(str).max()
            szse_latest = (
                szse_selected.loc[szse_selected["trade_date"].astype(str).eq(szse_latest_date)]
                .drop_duplicates("instrument_code", keep="last")
            )
            for row in szse_latest.itertuples():
                name = str(row.instrument_name)
                category, asset_class, classification_source = _etf_dimensions(
                    name, row.investment_type
                )
                instruments.append(
                    {
                        "code": str(row.instrument_code),
                        "name": name,
                        "market": "SZSE",
                        "category": category,
                        "assetClass": asset_class,
                        "classificationSource": classification_source,
                        "shares": None if pd.isna(row.shares) else round(float(row.shares) / 100_000_000, 2),
                        "nav": None if pd.isna(row.nav) else round(float(row.nav), 4),
                        "periods": {period: None for period in available_periods},
                        "coverage": 1,
                    }
                )
    instruments.sort(
        key=lambda item: abs(item["periods"].get("1d") or 0), reverse=True
    )

    daily_series = []
    for previous, current in list(zip(available_dates, available_dates[1:]))[-60:]:
        comparable = pivot[[previous, current]].dropna()
        delta = comparable[current] - comparable[previous]
        daily_series.append(
            {
                "date": current[5:],
                "value": round(float(delta.sum()) / 100_000_000, 2),
                "coverage": int(comparable.shape[0]),
            }
        )

    groups: dict[str, dict[str, float]] = {}
    for item in instruments:
        if all(item["periods"].get(period) is None for period in available_periods):
            continue
        category = item["category"]
        aggregate = groups.setdefault(
            category, {period: 0.0 for period in available_periods}
        )
        for period in available_periods:
            value = item["periods"].get(period)
            if value is not None:
                aggregate[period] += value
    group_periods = [
        {
            "name": name,
            "periods": {key: round(value, 2) for key, value in periods.items()},
        }
        for name, periods in groups.items()
    ]
    group_periods.sort(
        key=lambda item: abs(item["periods"].get("1d", 0)), reverse=True
    )

    market_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for item in instruments:
        market_counts[item["market"]] = market_counts.get(item["market"], 0) + 1
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1

    return {
        "dailySeries": daily_series,
        "instruments": instruments,
        "groupPeriods": group_periods,
        "availablePeriods": available_periods,
        "classification": "官方投资类型优先，基金简称关键词补充",
        "classificationVersion": "2026-07-v2",
        "universe": {
            "total": len(instruments),
            "marketCounts": market_counts,
            "categoryCounts": category_counts,
            "periodComparable": sum(
                1 for item in instruments if item["periods"].get("1d") is not None
            ),
        },
    }


def _build_connect_series(frame: pd.DataFrame, dates: list[str]) -> list[dict[str, Any]]:
    def total(rows: pd.DataFrame, field: str) -> float:
        if field not in rows.columns:
            return 0.0
        return float(pd.to_numeric(rows[field], errors="coerce").fillna(0).sum())

    series = []
    for trade_date in dates:
        daily = frame[frame["trade_date"].astype(str).eq(trade_date)]
        southbound = daily[daily["direction"].eq("southbound")]
        northbound = daily[daily["direction"].eq("northbound")]
        if daily.empty:
            continue
        south_by_market = {
            str(row.market): float(row.net_buy) / 100_000_000
            for row in southbound.itertuples()
        }
        series.append(
            {
                "date": trade_date[5:],
                "southboundNet": round(float(southbound["net_buy"].sum()) / 100_000_000, 2),
                "southboundTurnover": round(float(southbound["total_turnover"].sum()) / 100_000_000, 2),
                "southboundSseNet": round(south_by_market.get("SSE", 0.0), 2),
                "southboundSzseNet": round(south_by_market.get("SZSE", 0.0), 2),
                "northboundTurnover": round(total(northbound, "total_turnover") / 100_000_000, 2),
                "northboundEtfTurnover": round(total(northbound, "etf_turnover") / 100_000_000, 2),
                "northboundTradeCount": int(total(northbound, "total_trade_count")),
            }
        )
    return series


def build_dashboard_snapshot(
    *, manifest_path: Path, quality_path: Path, workspace_root: Path
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    paths = manifest["combined_paths"]
    dates = sorted(manifest["requested_dates"])
    as_of = dates[-1]

    sse_etf = _read_csv(workspace_root, paths["sse_etf"])
    szse_etf = (
        _read_csv(workspace_root, paths["szse_etf"])
        if paths.get("szse_etf")
        else pd.DataFrame()
    )
    sse_margin = _read_csv(workspace_root, paths["sse_margin"])
    szse_margin = _read_csv(workspace_root, paths["szse_margin"])
    connect = _read_csv(workspace_root, paths["stock_connect_summary"])

    etf_flows, etf_summary = _latest_etf_flows(sse_etf, dates)
    etf_terminal = _build_etf_terminal_data(sse_etf, szse_etf, dates)
    if "market" not in sse_margin.columns:
        sse_margin = sse_margin.assign(market="SSE")
    if "market" not in szse_margin.columns:
        szse_margin = szse_margin.assign(market="SZSE")
    margin = pd.concat([sse_margin, szse_margin], ignore_index=True)
    margin_columns = [
        "financing_balance_yuan",
        "financing_buy_yuan",
        "securities_lending_balance_yuan",
        "securities_lending_sell_volume_shares",
        "margin_total_balance_yuan",
    ]
    for column in margin_columns:
        if column not in margin.columns:
            margin[column] = 0
        margin[column] = pd.to_numeric(margin[column], errors="coerce").fillna(0)
    margin_daily = (
        margin.groupby("trade_date", as_index=False)
        .agg(
            financing_balance_yuan=("financing_balance_yuan", "sum"),
            financing_buy_yuan=("financing_buy_yuan", "sum"),
            securities_lending_balance_yuan=("securities_lending_balance_yuan", "sum"),
            securities_lending_sell_volume_shares=("securities_lending_sell_volume_shares", "sum"),
            margin_total_balance_yuan=("margin_total_balance_yuan", "sum"),
        )
        .sort_values("trade_date")
    )
    market_balance = (
        margin.pivot_table(
            index="trade_date",
            columns="market",
            values="financing_balance_yuan",
            aggfunc="sum",
        )
        .fillna(0)
        .to_dict("index")
    )
    latest_margin = float(margin_daily.iloc[-1]["financing_balance_yuan"])
    previous_margin = float(margin_daily.iloc[-2]["financing_balance_yuan"])
    three_day_start = float(margin_daily.iloc[0]["financing_balance_yuan"])

    connect_latest = connect[connect["trade_date"].astype(str).eq(as_of)].copy()
    southbound = connect_latest[connect_latest["direction"].eq("southbound")]
    northbound = connect_latest[connect_latest["direction"].eq("northbound")]
    southbound_net = (
        None
        if southbound.empty
        else float(southbound["net_buy"].sum()) / 100_000_000
    )
    northbound_turnover = (
        None
        if northbound.empty
        else float(northbound["total_turnover"].sum()) / 100_000_000
    )

    market_labels = {
        ("SSE", "southbound"): "沪港通南向",
        ("SZSE", "southbound"): "深港通南向",
        ("SSE", "northbound"): "沪股通北向",
        ("SZSE", "northbound"): "深股通北向",
    }
    connect_rows = []
    for direction in ("southbound", "northbound"):
        for market in ("SSE", "SZSE"):
            rows = connect_latest[
                connect_latest["direction"].eq(direction)
                & connect_latest["market"].eq(market)
            ]
            if rows.empty:
                connect_rows.append(
                    {
                        "name": market_labels[(market, direction)],
                        "value": "—",
                        "unit": "亿港元" if direction == "southbound" else "亿元",
                        "description": "当日互联互通休市或无披露",
                        "tone": "neutral",
                    }
                )
                continue
            row = rows.iloc[0]
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

    margin_series = []
    previous_balance: float | None = None
    for row in margin_daily.tail(60).itertuples():
        trade_date = str(row.trade_date)
        balance = float(row.financing_balance_yuan) / 100_000_000
        split = market_balance.get(row.trade_date, market_balance.get(trade_date, {}))
        margin_series.append(
            {
                "date": trade_date[5:],
                "balance": round(balance, 2),
                "label": f"{float(row.financing_balance_yuan) / 1_000_000_000_000:.4f} 万亿",
                "change": None if previous_balance is None else round(balance - previous_balance, 2),
                "financingBuy": round(float(row.financing_buy_yuan) / 100_000_000, 2),
                "lendingBalance": round(float(row.securities_lending_balance_yuan) / 100_000_000, 2),
                "lendingSellVolume": round(float(row.securities_lending_sell_volume_shares) / 100_000_000, 2),
                "totalBalance": round(float(row.margin_total_balance_yuan) / 100_000_000, 2),
                "sseBalance": round(float(split.get("SSE", 0)) / 100_000_000, 2),
                "szseBalance": round(float(split.get("SZSE", 0)) / 100_000_000, 2),
            }
        )
        previous_balance = balance
    counts = quality["counts"]
    szse_date = (
        None
        if szse_etf.empty or "trade_date" not in szse_etf.columns
        else str(szse_etf["trade_date"].max())
    )
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
                "eyebrow": "上交所 ETF 份额变化",
                "value": _signed(etf_summary["net"]),
                "unit": "亿份",
                "delta": f"{etf_summary['increasers']} 只增加 · {etf_summary['decreasers']} 只减少",
                "tone": "positive" if etf_summary["net"] >= 0 else "negative",
                "note": "相邻官方快照份额差，不是资金金额",
                "basis": {
                    "kind": "calculated",
                    "label": "公式计算",
                    "detail": "当日官方份额 − 前一交易日官方份额",
                },
            },
            {
                "id": "southbound",
                "eyebrow": "南向净买入",
                "value": "—" if southbound_net is None else _signed(southbound_net),
                "unit": "亿港元",
                "delta": f"沪港通 {south_market_values['沪港通南向']} · 深港通 {south_market_values['深港通南向']}",
                "tone": (
                    "neutral"
                    if southbound_net is None
                    else ("positive" if southbound_net >= 0 else "negative")
                ),
                "note": (
                    "当日互联互通休市或无披露"
                    if southbound_net is None
                    else "官方买入额 − 卖出额"
                ),
                "basis": {
                    "kind": "calculated",
                    "label": "公式计算",
                    "detail": "港交所官方买入成交额 − 卖出成交额",
                },
            },
            {
                "id": "margin",
                "eyebrow": "沪深融资余额",
                "value": f"{latest_margin / 1_000_000_000_000:.4f}",
                "unit": "万亿元",
                "delta": f"较前日 {_signed((latest_margin - previous_margin) / 100_000_000)} 亿元",
                "tone": "positive" if latest_margin >= previous_margin else "negative",
                "note": "沪深两市合计",
                "basis": {
                    "kind": "official",
                    "label": "官方汇总",
                    "detail": "上交所与深交所融资余额合计",
                },
            },
            {
                "id": "northbound",
                "eyebrow": "北向成交总额",
                "value": "—" if northbound_turnover is None else f"{northbound_turnover:,.2f}",
                "unit": "亿元",
                "delta": f"沪股通 {north_rows[0]['value']} · 深股通 {north_rows[1]['value']}",
                "tone": "neutral",
                "note": "当前官方不披露北向净流入",
                "basis": {
                    "kind": "official",
                    "label": "官方汇总",
                    "detail": "港交所北向成交总额，不推断资金方向",
                },
            },
        ],
        "etfFlows": etf_flows,
        "etfTerminal": etf_terminal,
        "marginSeries": margin_series,
        "connect": connect_rows,
        "connectSeries": _build_connect_series(connect, dates[-60:]),
        "dataStatus": [
            {"name": "上交所 ETF", "status": "ready", "asOf": as_of[5:], "detail": f"官方日期 · {etf_summary['latest_count']} 只 · {len(dates)} 日窗口", "basis": "official"},
            {
                "name": "深交所 ETF",
                "status": "warning" if szse_date is None else "ready",
                "asOf": "—" if szse_date is None else szse_date[5:],
                "detail": (
                    "官方日列表不支持历史回填"
                    if szse_date is None
                    else "官方列表页规模日期"
                ),
                "basis": "official",
            },
            {"name": "融资融券", "status": "ready", "asOf": as_of[5:], "detail": "官方沪深汇总", "basis": "official"},
            {
                "name": "互联互通",
                "status": "warning" if connect_latest.empty else "ready",
                "asOf": as_of[5:],
                "detail": (
                    "当日互联互通休市或无披露"
                    if connect_latest.empty
                    else "南向复算 · 北向官方成交"
                ),
                "basis": "mixed",
            },
        ],
    }
