"""Canonical ETF facts shared by dashboard producers and release QA.

The collectors keep Shanghai and Shenzhen responses in separate files because
their source contracts and publication clocks differ.  Once both responses
enter the derived layer, however, the business key is always
``trade_date + market + instrument_code``.  This module is the single place
where that merge, normalization and duplicate gate is defined.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


CANONICAL_ETF_COLUMNS = (
    "trade_date",
    "market",
    "instrument_code",
    "instrument_name",
    "shares",
    "nav",
    "nav_date",
    "etf_type",
    "investment_type",
)
CANONICAL_ETF_KEY = ("trade_date", "market", "instrument_code")


def _normalise_source(source: pd.DataFrame, market: str, dates: list[str]) -> pd.DataFrame:
    selected = source.reindex(columns=[column for column in CANONICAL_ETF_COLUMNS if column != "market"]).copy()
    selected["market"] = market

    def text(value: Any) -> str:
        return "" if pd.isna(value) else str(value).strip()

    selected["trade_date"] = selected["trade_date"].map(text)
    selected["instrument_code"] = (
        selected["instrument_code"]
        .map(text)
        .astype("string")
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    selected["instrument_name"] = selected["instrument_name"].map(text)
    selected["shares"] = pd.to_numeric(selected["shares"], errors="coerce")
    selected["nav"] = pd.to_numeric(selected["nav"], errors="coerce")
    selected["nav_date"] = selected["nav_date"].map(
        lambda value: None if pd.isna(value) or not str(value).strip() else str(value).strip()
    )
    selected["etf_type"] = selected["etf_type"].map(text)
    selected["investment_type"] = selected["investment_type"].map(text)
    requested_dates = {str(value) for value in dates}
    return selected.loc[
        selected["trade_date"].isin(requested_dates)
        & selected["instrument_code"].str.fullmatch(r"\d{6}")
    ].reindex(columns=CANONICAL_ETF_COLUMNS)


def build_canonical_etf_daily_fact(
    sse_frame: pd.DataFrame,
    szse_frame: pd.DataFrame,
    dates: list[str],
) -> pd.DataFrame:
    """Return the deterministic combined ETF daily fact table.

    Duplicate business keys are a hard error.  Silently keeping the last row
    would make a source correction indistinguishable from a duplicated raw
    response and could change all downstream flow values without a QA signal.
    """

    parts = [
        _normalise_source(sse_frame, "SSE", dates),
        _normalise_source(szse_frame, "SZSE", dates),
    ]
    selected = pd.concat(parts, ignore_index=True).reindex(columns=CANONICAL_ETF_COLUMNS)
    if selected.empty:
        return selected
    duplicates = selected.loc[selected.duplicated(list(CANONICAL_ETF_KEY), keep=False)]
    if not duplicates.empty:
        keys = sorted(
            {
                ":".join(str(row[column]) for column in CANONICAL_ETF_KEY)
                for _, row in duplicates.iterrows()
            }
        )
        sample = ",".join(keys[:20])
        suffix = "..." if len(keys) > 20 else ""
        raise ValueError(f"canonical_etf_duplicate_business_key:{sample}{suffix}")
    return selected.sort_values(list(CANONICAL_ETF_KEY), kind="mergesort").reset_index(drop=True)


def latest_etf_flows_from_terminal(
    terminal: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Derive the homepage headline and Top 5 rows from one ETF terminal."""

    changes: list[tuple[dict[str, Any], float]] = []
    for item in terminal.get("instruments") or []:
        if not isinstance(item, dict):
            continue
        periods = item.get("periods")
        if not isinstance(periods, dict) or periods.get("1d") is None:
            continue
        try:
            value = float(periods["1d"])
        except (TypeError, ValueError):
            continue
        if not pd.notna(value):
            continue
        changes.append((item, value))

    def rows(ascending: bool) -> list[dict[str, Any]]:
        ordered = sorted(changes, key=lambda pair: pair[1], reverse=not ascending)[:5]
        return [
            {
                "code": str(item.get("code") or "").zfill(6),
                "name": str(item.get("name") or item.get("code") or ""),
                "value": round(value, 2),
            }
            for item, value in ordered
        ]

    net = sum(value for _, value in changes)
    return (
        {"inflow": rows(False), "outflow": rows(True)},
        {
            "net": float(net),
            "increasers": sum(value > 0 for _, value in changes),
            "decreasers": sum(value < 0 for _, value in changes),
            "latest_count": len(changes),
        },
    )
