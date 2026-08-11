"""Date-safe ETF-to-stock penetration calculations.

The calculation in this module is deliberately pure and offline.  ETF share
snapshots and the exchange's PCF (portfolio composition file) are joined only
when their effective date and market/code keys match.  A PCF is either
published on the share date or published on the next trading day with an
explicit ``PRE_TRADING_DAY`` equal to that share date.  A missing, stale or
unrelated PCF never falls back to the latest available file: the result is
explicitly marked unavailable instead.

This is an *estimated stock delivery/redemption equivalent*, not a claim about
secondary-market cash flow.  Cash-substitution flags are retained as a caveat
because an ETF creation/redemption can be settled with cash rather than the
underlying shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


class StockFlowInputError(ValueError):
    """Raised when a source contains an ambiguous or unsafe business key."""


@dataclass(frozen=True)
class StockFlowLimits:
    """Payload limits applied after the full offline aggregation."""

    max_stocks: int = 500
    max_contributors: int = 20


REQUIRED_SHARE_COLUMNS = {"trade_date", "market", "instrument_code", "shares"}
REQUIRED_PCF_COLUMNS = {
    "trade_date",
    "market",
    "etf_code",
    "stock_code",
    "quantity",
    "creation_unit",
}


def _date_series(frame: pd.DataFrame, column: str) -> pd.Series:
    parsed = pd.to_datetime(frame[column], errors="coerce")
    if parsed.isna().any():
        raise StockFlowInputError(f"invalid_date:{column}")
    return parsed.dt.strftime("%Y-%m-%d")


def _code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6)


def _empty_result(
    *,
    as_of: str,
    previous_date: str | None,
    pcf_date: str | None,
    warnings: list[str],
    reason: str,
) -> dict[str, Any]:
    return {
        "available": False,
        "unit": "股",
        "asOf": as_of,
        "basis": {
            "shareDate": as_of,
            "previousShareDate": previous_date,
            "pcfDate": pcf_date,
            "pcfAlignment": "unavailable",
            "timezone": "Asia/Shanghai",
            "calculation": "ETF份额变化 ÷ 最小申赎单位 × PCF成分数量",
            "source": "SSE ETF PCF + SSE/SZSE ETF official share snapshots",
        },
        "coverage": {
            "shareEtfCount": 0,
            "pcfEtfCount": 0,
            "matchedEtfCount": 0,
            "stockCount": 0,
            "shownStockCount": 0,
            "limited": False,
        },
        "stocks": [],
        "etfs": [],
        "warnings": [reason, *warnings],
    }


def _normalise_share_frame(frame: pd.DataFrame, *, as_of: str, previous_date: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=sorted(REQUIRED_SHARE_COLUMNS))
    missing = REQUIRED_SHARE_COLUMNS - set(frame.columns)
    if missing:
        raise StockFlowInputError(f"share_columns_missing:{','.join(sorted(missing))}")
    value = frame.copy()
    value["trade_date"] = _date_series(value, "trade_date")
    value["market"] = value["market"].astype(str).str.upper().str.strip()
    value["instrument_code"] = value["instrument_code"].map(_code)
    value["shares"] = pd.to_numeric(value["shares"], errors="coerce")
    if value["shares"].isna().any() or value["shares"].lt(0).any():
        raise StockFlowInputError("invalid_share_value")
    value = value[value["trade_date"].isin({as_of, previous_date})].copy()
    if value.empty:
        return value
    key = ["trade_date", "market", "instrument_code"]
    if value.duplicated(key).any():
        raise StockFlowInputError("duplicate_share_snapshot_key")
    return value


def _normalise_pcf_frame(frame: pd.DataFrame, *, as_of: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=sorted(REQUIRED_PCF_COLUMNS))
    missing = REQUIRED_PCF_COLUMNS - set(frame.columns)
    if missing:
        raise StockFlowInputError(f"pcf_columns_missing:{','.join(sorted(missing))}")
    value = frame.copy()
    value["trade_date"] = _date_series(value, "trade_date")
    # Older archived PCF rows predate the explicit publication/effective-date
    # split.  Treat their trade date as both fields so historical evidence
    # remains readable while new captures retain the official PRE_TRADING_DAY
    # relationship.
    if "pcf_date" not in value.columns:
        value["pcf_date"] = value["trade_date"]
    else:
        value["pcf_date"] = _date_series(value, "pcf_date")
    if "pcf_previous_date" not in value.columns:
        value["pcf_previous_date"] = value["trade_date"]
    else:
        value["pcf_previous_date"] = _date_series(value, "pcf_previous_date")
    value["market"] = value["market"].astype(str).str.upper().str.strip()
    value["etf_code"] = value["etf_code"].map(_code)
    value["stock_code"] = value["stock_code"].map(_code)
    value["quantity"] = pd.to_numeric(value["quantity"], errors="coerce")
    value["creation_unit"] = pd.to_numeric(value["creation_unit"], errors="coerce")
    if value["quantity"].isna().any() or value["quantity"].le(0).any():
        raise StockFlowInputError("invalid_pcf_quantity")
    if value["creation_unit"].isna().any() or value["creation_unit"].le(0).any():
        raise StockFlowInputError("invalid_pcf_creation_unit")
    value = value[value["trade_date"].eq(as_of)].copy()
    if value.empty:
        return value
    key = ["trade_date", "market", "etf_code", "stock_code"]
    if value.duplicated(key).any():
        raise StockFlowInputError("duplicate_pcf_component_key")
    # A fund's creation unit is an ETF-level field repeated on each component.
    units = value.groupby(["market", "etf_code"])["creation_unit"].nunique(dropna=True)
    if (units > 1).any():
        raise StockFlowInputError("inconsistent_pcf_creation_unit")
    return value


def build_stock_flow(
    share_frame: pd.DataFrame,
    pcf_frame: pd.DataFrame,
    *,
    as_of: str,
    previous_date: str | None = None,
    pcf_date: str | None = None,
    limits: StockFlowLimits | None = None,
) -> dict[str, Any]:
    """Build a bounded, auditable ETF-to-stock flow read model.

    ``share_frame`` contains official ETF total-share snapshots for the current
    and previous trading dates. ``pcf_frame`` contains PCF component rows with
    an effective ``trade_date`` and a repeated ``creation_unit`` per ETF.  The
    returned ``stocks``
    list is sorted by absolute estimated stock-share flow, while ``coverage``
    retains full-universe totals so truncation cannot masquerade as a total.
    """

    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    limits = limits or StockFlowLimits()
    if limits.max_stocks <= 0 or limits.max_contributors <= 0:
        raise StockFlowInputError("invalid_payload_limits")

    shares = share_frame.copy()
    if previous_date is None and "trade_date" in shares.columns:
        candidate_dates = sorted(
            pd.to_datetime(shares["trade_date"], errors="coerce")
            .dropna()
            .dt.strftime("%Y-%m-%d")
            .unique()
        )
        previous_date = next((value for value in reversed(candidate_dates) if value < as_of), None)
    if previous_date is None:
        return _empty_result(
            as_of=as_of,
            previous_date=None,
            pcf_date=pcf_date,
            warnings=[],
            reason="previous_share_date_missing",
        )
    previous_date = pd.Timestamp(previous_date).strftime("%Y-%m-%d")
    if pcf_date is not None:
        pcf_date = pd.Timestamp(pcf_date).strftime("%Y-%m-%d")

    shares = _normalise_share_frame(shares, as_of=as_of, previous_date=previous_date)
    pcf = _normalise_pcf_frame(pcf_frame, as_of=as_of)
    pcf_dates = sorted(
        pd.to_datetime(pcf["pcf_date"], errors="coerce")
        .dropna()
        .dt.strftime("%Y-%m-%d")
        .unique()
    ) if "pcf_date" in pcf.columns else []
    if pcf_date is None and len(pcf_dates) == 1:
        pcf_date = pcf_dates[0]
    effective_dates = sorted(
        pcf["trade_date"].dropna().astype(str).unique().tolist()
    ) if "trade_date" in pcf.columns else []
    previous_dates = sorted(
        pcf["pcf_previous_date"].dropna().astype(str).unique().tolist()
    ) if "pcf_previous_date" in pcf.columns else []
    aligned_previous_day = (
        pcf_date is not None
        and pcf_date > as_of
        and previous_dates == [as_of]
    )
    if not effective_dates or effective_dates != [as_of] or (
        pcf_date != as_of and not aligned_previous_day
    ):
        return _empty_result(
            as_of=as_of,
            previous_date=previous_date,
            pcf_date=pcf_date,
            warnings=[],
            reason="pcf_date_mismatch",
        )
    if pcf.empty:
        return _empty_result(
            as_of=as_of,
            previous_date=previous_date,
            pcf_date=pcf_date,
            warnings=[],
            reason="aligned_pcf_missing",
        )

    share_key = ["market", "instrument_code"]
    current = shares[shares["trade_date"].eq(as_of)][[*share_key, "shares"]].rename(
        columns={"shares": "current_shares"}
    )
    previous = shares[shares["trade_date"].eq(previous_date)][[*share_key, "shares"]].rename(
        columns={"shares": "previous_shares"}
    )
    deltas = current.merge(previous, on=share_key, how="inner")
    if deltas.empty:
        return _empty_result(
            as_of=as_of,
            previous_date=previous_date,
            pcf_date=pcf_date,
            warnings=[],
            reason="share_snapshots_not_comparable",
        )
    deltas["share_delta"] = deltas["current_shares"] - deltas["previous_shares"]
    pcf_etfs = pcf[["market", "etf_code"]].drop_duplicates()
    matched = deltas.merge(
        pcf_etfs,
        left_on=["market", "instrument_code"],
        right_on=["market", "etf_code"],
        how="inner",
    ).drop(columns=["etf_code"])
    if matched.empty:
        return _empty_result(
            as_of=as_of,
            previous_date=previous_date,
            pcf_date=pcf_date,
            warnings=[],
            reason="no_share_changed_etf_has_aligned_pcf",
        )

    joined = pcf.merge(
        matched[["market", "instrument_code", "share_delta", "current_shares"]],
        left_on=["market", "etf_code"],
        right_on=["market", "instrument_code"],
        how="inner",
    ).drop(columns=["instrument_code"])
    joined["stock_flow_shares"] = joined["share_delta"] / joined["creation_unit"] * joined["quantity"]
    joined["current_penetrated_shares"] = joined["current_shares"] / joined["creation_unit"] * joined["quantity"]
    joined["stock_code"] = joined["stock_code"].map(_code)
    stock_names = joined["stock_name"] if "stock_name" in joined.columns else joined["stock_code"]
    etf_names = joined["etf_name"] if "etf_name" in joined.columns else joined["etf_code"]
    substitution_flags = joined["substitution_flag"] if "substitution_flag" in joined.columns else pd.Series("", index=joined.index)
    joined["stock_name"] = stock_names.fillna(joined["stock_code"])
    joined["etf_name"] = etf_names.fillna(joined["etf_code"])
    joined["substitution_flag"] = substitution_flags.fillna("").astype(str)

    warnings: list[str] = []
    pcf_alignment = "same_trading_day" if pcf_date == as_of else "next_day_previous_trade_date"
    if pcf_alignment == "next_day_previous_trade_date":
        warnings.append(
            f"PCF 发布日 {pcf_date} 通过 PRE_TRADING_DAY 对齐到份额日 {as_of}；"
            "个股数量为次日披露篮子等价估算"
        )
    if joined["substitution_flag"].str.strip().ne("").any():
        warnings.append("PCF含现金替代标记；个股数量是申赎篮子等价估算，不等同实际交收")

    stock_groups: list[dict[str, Any]] = []
    for (market, stock_code), rows in joined.groupby(["market", "stock_code"], sort=False):
        contributions = []
        for row in rows.sort_values("stock_flow_shares", key=lambda series: series.abs(), ascending=False).head(limits.max_contributors).itertuples():
            contribution = float(row.stock_flow_shares)
            contributions.append(
                {
                    "etfCode": str(row.etf_code),
                    "etfName": str(row.etf_name),
                    "shareDelta": round(float(row.share_delta), 2),
                    "quantity": int(round(float(row.quantity))),
                    "creationUnit": int(round(float(row.creation_unit))),
                    "stockFlowShares": round(contribution, 2),
                    "currentPenetratedShares": round(float(row.current_penetrated_shares), 2),
                    "estimated": True,
                }
            )
        total = float(rows["stock_flow_shares"].sum())
        stock_groups.append(
            {
                "market": str(market),
                "stockCode": str(stock_code),
                "stockName": str(rows.iloc[0]["stock_name"]),
                "netShares": round(total, 2),
                "inflowShares": round(float(rows.loc[rows["stock_flow_shares"] > 0, "stock_flow_shares"].sum()), 2),
                "outflowShares": round(float(-rows.loc[rows["stock_flow_shares"] < 0, "stock_flow_shares"].sum()), 2),
                "etfCount": int(rows["etf_code"].nunique()),
                "contributors": contributions,
            }
        )
    stock_groups.sort(key=lambda item: abs(item["netShares"]), reverse=True)
    shown_stocks = stock_groups[: limits.max_stocks]

    etf_rows: list[dict[str, Any]] = []
    for (market, etf_code), rows in joined.groupby(["market", "etf_code"], sort=False):
        row = rows.iloc[0]
        etf_rows.append(
            {
                "market": str(market),
                "code": str(etf_code),
                "name": str(row["etf_name"]),
                "shareDelta": round(float(row["share_delta"]), 2),
                "creationUnit": int(round(float(row["creation_unit"]))),
                "stockCount": int(rows["stock_code"].nunique()),
                "estimated": True,
            }
        )
    etf_rows.sort(key=lambda item: abs(item["shareDelta"]), reverse=True)
    total_flow = float(joined["stock_flow_shares"].sum())
    return {
        "available": True,
        "unit": "股",
        "asOf": as_of,
        "basis": {
            "shareDate": as_of,
            "previousShareDate": previous_date,
            "pcfDate": pcf_date,
            "pcfAlignment": pcf_alignment,
            "timezone": "Asia/Shanghai",
            "calculation": "ETF份额变化 ÷ 最小申赎单位 × PCF成分数量",
            "source": "SSE ETF PCF + SSE/SZSE ETF official share snapshots",
            "cashSubstitution": "含现金替代标记时，仅作申赎篮子等价估算",
        },
        "coverage": {
            "shareEtfCount": int(deltas.shape[0]),
            "pcfEtfCount": int(pcf_etfs.shape[0]),
            "matchedEtfCount": int(matched.shape[0]),
            "stockCount": len(stock_groups),
            "shownStockCount": len(shown_stocks),
            "limited": len(shown_stocks) < len(stock_groups),
            "netSharesTotal": round(total_flow, 2),
            "shownNetShares": round(float(sum(item["netShares"] for item in shown_stocks)), 2),
        },
        "stocks": shown_stocks,
        "etfs": etf_rows,
        "warnings": warnings,
    }
