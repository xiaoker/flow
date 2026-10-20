from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

from .adapters.official import (
    SSE_MARGIN_SECURITY,
    parse_bse_margin_summary,
    parse_hkex_tab_data,
)
from .dashboard import build_dashboard_snapshot
from .etf_flow_state import materialize_dashboard_snapshot
from .etf_facts import build_canonical_etf_daily_fact
from .etf_stock_flow import normalise_underlying_code
from .instrument_names import clean_instrument_name, resolve_hkex_instrument_name
from .models import QualityFinding
from .quality import summarize_findings
from .read_models import build_view_payloads
from .storage import now_iso, write_json


APPROVED_SOURCE_HOSTS = {
    "sse": {"query.sse.com.cn", "www.sse.com.cn"},
    "szse": {"fund.szse.cn", "www.szse.cn"},
    # New official disclosure host plus the legacy host used by archived
    # evidence.  Historical artifacts must remain auditable after the host
    # migration.
    "bse": {"www.bse.cn", "www.bseinfo.net"},
    "hkex": {"www.hkex.com.hk"},
}

COMBINED_DATASET_KEYS = {
    ("sse", "etf_scale"): ("sse_etf",),
    ("szse", "etf_scale"): ("szse_etf",),
    ("sse", "margin_market_daily"): ("sse_margin",),
    ("szse", "margin_market_daily"): ("szse_margin",),
    ("bse", "margin_market_daily"): ("bse_margin",),
    ("hkex", "stock_connect_daily"): (
        "stock_connect_summary",
        "stock_connect_top",
    ),
}

EXPECTED_CONTEXT = {
    "sse_etf": ("sse", {"SSE"}),
    "szse_etf": ("szse", {"SZSE"}),
    "sse_margin": ("sse", {"SSE"}),
    "szse_margin": ("szse", {"SZSE"}),
    "stock_connect_summary": ("hkex", {"SSE", "SZSE"}),
    "stock_connect_top": ("hkex", {"SSE", "SZSE"}),
}


def _finding(
    check_id: str,
    dataset: str,
    passed: bool,
    *,
    message: str,
    evidence: dict[str, Any],
    severity: str = "critical",
    warn_only: bool = False,
    remediation: str | None = None,
) -> QualityFinding:
    return QualityFinding(
        check_id=check_id,
        dataset=dataset,
        status="pass" if passed else ("warn" if warn_only else "fail"),
        severity="info" if passed else severity,  # type: ignore[arg-type]
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


def _paths(value: str | None) -> list[str]:
    return [] if not value else [part for part in value.split(";") if part]


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(
            path,
            dtype={
                "instrument_code": str,
                "etf_code": str,
                "stock_code": str,
                "listing_market": str,
            },
        )
    except pd.errors.EmptyDataError:
        # Legacy closed-day partitions contain no header. Retain zero rows;
        # required coverage/schema checks still fail unless raw proof allows it.
        return pd.DataFrame()


def _number(value: Any, multiplier: float = 1.0) -> float | None:
    if value is None or str(value).strip() in {"", "-", "--", "N/A", "NA"}:
        return None
    return float(str(value).replace(",", "").strip()) * multiplier


def _normalized_date(value: Any, fallback: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return fallback if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def _compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    keys: list[str],
    text_columns: list[str],
    numeric_columns: list[str],
) -> dict[str, Any]:
    required = set(keys + text_columns + numeric_columns)
    missing_columns = sorted(required - set(actual.columns))
    if missing_columns:
        return {
            "matched": False,
            "expected_rows": len(expected),
            "actual_rows": len(actual),
            "missing_columns": missing_columns,
            "missing_keys": len(expected),
            "extra_keys": len(actual),
            "value_mismatches": len(expected),
        }

    def indexed(frame: pd.DataFrame) -> dict[tuple[str, ...], pd.Series]:
        rows: dict[tuple[str, ...], pd.Series] = {}
        for _, row in frame.iterrows():
            key = tuple(str(row[column]) for column in keys)
            rows[key] = row
        return rows

    expected_rows = indexed(expected)
    actual_rows = indexed(actual)
    expected_keys = set(expected_rows)
    actual_keys = set(actual_rows)
    value_mismatches = 0
    sample_mismatches: list[dict[str, Any]] = []
    for key in sorted(expected_keys & actual_keys):
        expected_row = expected_rows[key]
        actual_row = actual_rows[key]
        row_mismatches: list[str] = []
        for column in text_columns:
            left = "" if pd.isna(expected_row[column]) else str(expected_row[column]).strip()
            right = "" if pd.isna(actual_row[column]) else str(actual_row[column]).strip()
            if left != right:
                row_mismatches.append(column)
        for column in numeric_columns:
            left = expected_row[column]
            right = actual_row[column]
            if pd.isna(left) and pd.isna(right):
                continue
            if pd.isna(left) or pd.isna(right) or not math.isclose(
                float(left), float(right), rel_tol=1e-12, abs_tol=0.01
            ):
                row_mismatches.append(column)
        if row_mismatches:
            value_mismatches += 1
            if len(sample_mismatches) < 5:
                sample_mismatches.append({"key": list(key), "columns": row_mismatches})

    missing_keys = expected_keys - actual_keys
    extra_keys = actual_keys - expected_keys
    return {
        "matched": not missing_columns and not missing_keys and not extra_keys and value_mismatches == 0,
        "expected_rows": len(expected),
        "actual_rows": len(actual),
        "missing_columns": missing_columns,
        "missing_keys": len(missing_keys),
        "extra_keys": len(extra_keys),
        "value_mismatches": value_mismatches,
        "sample_mismatches": sample_mismatches,
    }


def _expected_sse_etf(content: bytes, requested_date: str) -> pd.DataFrame:
    rows = json.loads(content).get("result") or []
    return pd.DataFrame(
        [
            {
                "trade_date": _normalized_date(row.get("STAT_DATE"), requested_date),
                "market": "SSE",
                "instrument_code": str(row.get("SEC_CODE", "")).zfill(6),
                "instrument_name": row.get("SEC_NAME"),
                "shares": _number(row.get("TOT_VOL"), 10_000),
            }
            for row in rows
        ]
    )


def _expected_sse_etf_pcf(content: bytes, requested_date: str) -> pd.DataFrame:
    """Rebuild the normalized PCF rows from the immutable compressed response."""

    payload_bytes = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
    payload = json.loads(payload_bytes)
    expected: list[dict[str, Any]] = []
    for response in payload.get("responses") or []:
        if not isinstance(response, dict):
            continue
        observed_date = response.get("observed_date")
        previous_trading_date = response.get("previous_trading_date")
        same_day = observed_date == requested_date
        previous_day = previous_trading_date == requested_date
        if not same_day and not previous_day:
            continue
        date_basis = (
            "sse_pcf_same_day_morning"
            if same_day
            else "sse_pcf_next_day_previous_trading_day"
        )
        basic = response.get("basic") or {}
        etf_code = str(response.get("code") or basic.get("FUNDID2") or "").strip().zfill(6)
        creation_unit = _number(
            basic.get("CREATION_REDEMPTION_UNIT") or basic.get("CREATION_UNIT")
        )
        if not re.fullmatch(r"\d{6}", etf_code) or creation_unit is None or creation_unit <= 0:
            continue
        etf_name = basic.get("FUND_NAME") or basic.get("FUND_FULL_NAME") or etf_code
        for component in response.get("components") or []:
            if not isinstance(component, dict):
                continue
            listing_market = str(
                component.get("UNDERLYION_SECURITY_ID")
                or component.get("UNDERLYING_SECURITY_ID")
                or ""
            ).strip()
            stock_code = normalise_underlying_code(
                str(
                    component.get("INSTRUMENT_ID")
                    or component.get("STOCK_CODE")
                    or component.get("UNDERLYING_SECURITY_ID")
                    or ""
                ).strip(),
                listing_market,
            )
            quantity = _number(component.get("QUANTITY"))
            if (
                not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", stock_code)
                or quantity is None
                or quantity <= 0
            ):
                continue
            expected_row = {
                "trade_date": requested_date,
                "pcf_date": observed_date,
                "market": "SSE",
                "etf_code": etf_code,
                "etf_name": etf_name,
                "stock_code": stock_code,
                "stock_name": component.get("INSTRUMENT_NAME") or stock_code,
                "quantity": quantity,
                "creation_unit": creation_unit,
                "substitution_flag": component.get("SUBSTITUTION_FLAG") or "",
                "listing_market": listing_market,
                "source_id": "sse",
                "source_grade": "A",
                "date_basis": date_basis,
            }
            if previous_trading_date:
                expected_row["pcf_previous_date"] = previous_trading_date
            expected.append(expected_row)
    return pd.DataFrame(expected)


def _expected_sse_etf_nav(content: bytes, requested_date: str) -> pd.DataFrame:
    """Rebuild SSE NAV rows using the collector's moving-publication rule.

    The SSE ETF list is a current snapshot.  When it advances before the
    requested share date is archived, the collector is allowed to align the
    next publication to the *latest* known share date only.  Keeping this
    reconstruction independent from the collector is important: QA must
    verify the rule rather than simply compare the normalized file with the
    same implementation that produced it.
    """

    payload = json.loads(content)
    known_dates = sorted(
        {
            parsed
            for value in payload.get("known_trading_dates") or []
            if (parsed := _normalized_date(value, ""))
        }
    )
    latest_known_date = known_dates[-1] if known_dates else requested_date
    rows: list[dict[str, Any]] = []
    for response in payload.get("responses") or []:
        for row in response.get("result") or []:
            code = str(row.get("FUNDID2") or "").zfill(6)
            nav = _number(str(row.get("NAV") or "").replace("￥", "").replace("¥", ""))
            trading_day = _normalized_date(row.get("TRADING_DAY"), "")
            if not re.fullmatch(r"\d{6}", code) or nav is None or not trading_day:
                continue
            if trading_day == requested_date:
                share_date = requested_date
            elif (
                requested_date == latest_known_date
                and trading_day > requested_date
                and (
                    pd.to_datetime(trading_day) - pd.to_datetime(requested_date)
                ).days
                <= 10
            ):
                share_date = requested_date
            else:
                continue
            rows.append(
                {
                    "trade_date": share_date,
                    "market": "SSE",
                    "instrument_code": code,
                    "instrument_name": row.get("ETF_FULLNAME") or code,
                    "nav": nav,
                }
            )
    return pd.DataFrame(rows).drop_duplicates(
        ["trade_date", "market", "instrument_code"], keep="last"
    )


def _expected_szse_etf(content: bytes, trade_date: str) -> pd.DataFrame:
    frame = pd.read_excel(io.BytesIO(content), engine="openpyxl", dtype={"基金代码": str})
    # The SZSE fund list is broader than the ETF universe.  The collector
    # deliberately keeps only rows whose official product type is ETF, so the
    # independent raw-to-normalized comparison must apply the same explicit
    # source rule before comparing keys.  Comparing the full workbook here
    # would reject every valid run as soon as LOF/REIT rows are present.
    product_type = frame.get("基金类别")
    if product_type is not None:
        frame = frame.loc[product_type.astype(str).str.strip().eq("ETF")].copy()
    return pd.DataFrame(
        {
            "trade_date": trade_date,
            "market": "SZSE",
            "instrument_code": frame["基金代码"].astype(str).str.zfill(6),
            "instrument_name": frame["基金简称"],
            "shares": pd.to_numeric(frame["当前规模(份)"].astype(str).str.replace(",", "", regex=False), errors="coerce"),
            "nav": pd.to_numeric(frame.get("净值"), errors="coerce"),
        }
    )


def _expected_szse_historical_etf(content: bytes, trade_date: str) -> pd.DataFrame:
    """Rebuild exact-date SZSE rows from the retained fund_jjgm responses."""

    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("SZSE historical ETF evidence is not JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("SZSE historical ETF evidence must be an object")
    if payload.get("source") != "深圳证券交易所" or payload.get("dataset") != "etf_scale":
        raise ValueError("SZSE historical ETF evidence has an invalid source identity")
    if payload.get("requestedDate") != trade_date:
        raise ValueError("SZSE historical ETF evidence date does not match the artifact")
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise ValueError("SZSE historical ETF evidence responses are missing")

    rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for entry in responses:
        if not isinstance(entry, dict):
            raise ValueError("SZSE historical ETF response entry is invalid")
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, list):
            raise ValueError("SZSE historical ETF response envelope is invalid")
        code = str(request.get("code") or "").strip().zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("SZSE historical ETF response has an invalid code")
        for block in response:
            if not isinstance(block, dict):
                continue
            data = block.get("data")
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or str(item.get("size_date") or "") != trade_date:
                    continue
                shares = _number(item.get("current_size"), multiplier=10_000)
                name = str(item.get("security_short_name") or "").strip()
                if shares is None or not name:
                    raise ValueError(f"SZSE historical ETF row is incomplete for {code}")
                key = (trade_date, "SZSE", code)
                if key in seen_keys:
                    raise ValueError(f"duplicate SZSE historical ETF row for {code}")
                seen_keys.add(key)
                rows.append(
                    {
                        "trade_date": trade_date,
                        "market": "SZSE",
                        "instrument_code": code,
                        "instrument_name": name,
                        "shares": shares,
                        "nav": None,
                    }
                )
    return pd.DataFrame(rows)


def _expected_sse_margin(content: bytes) -> pd.DataFrame:
    rows = json.loads(content).get("result") or []
    return pd.DataFrame(
        [
            {
                "trade_date": pd.to_datetime(row.get("opDate"), format="%Y%m%d").strftime("%Y-%m-%d"),
                "market": "SSE",
                "financing_balance_yuan": _number(row.get("rzye")),
                "financing_buy_yuan": _number(row.get("rzmre")),
                "securities_lending_balance_yuan": _number(row.get("rqylje")),
                "margin_total_balance_yuan": _number(row.get("rzrqjyzl")),
            }
            for row in rows
        ]
    )


def _expected_sse_margin_security(content: bytes, requested_date: str) -> pd.DataFrame:
    """Rebuild SSE per-security margin rows from the official response.

    The endpoint can return a moving/stale response.  Only rows whose
    ``opDate`` equals the requested date are eligible; stale rows are kept in
    the raw evidence but must not be relabeled during normalization.
    """

    payload = json.loads(content)
    page_help = payload.get("pageHelp") if isinstance(payload, dict) else None
    rows = page_help.get("data") if isinstance(page_help, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []

    expected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_date = _normalized_date(row.get("opDate"), "")
        if source_date != requested_date:
            continue
        code = str(row.get("stockCode") or "").strip().zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        expected.append(
            {
                "trade_date": requested_date,
                "source_trade_date": source_date,
                "market": "SSE",
                "stock_code": code,
                "security_name": row.get("securityAbbr") or code,
                "financing_balance_yuan": _number(row.get("rzye")),
                "financing_buy_yuan": _number(row.get("rzmre")),
                "financing_repayment_yuan": _number(row.get("rzche")),
                "securities_lending_balance_shares": _number(row.get("rqyl")),
                "securities_lending_sell_shares": _number(row.get("rqmcl")),
                "securities_lending_repayment_shares": _number(row.get("rqchl")),
                "source_id": SSE_MARGIN_SECURITY.source_id,
                "source_grade": SSE_MARGIN_SECURITY.source_grade,
                "date_basis": SSE_MARGIN_SECURITY.date_semantics,
            }
        )
    return pd.DataFrame(expected)


def _expected_szse_margin(content: bytes, requested_date: str) -> pd.DataFrame:
    payload = json.loads(content)
    rows = payload[0].get("data", []) if payload else []
    return pd.DataFrame(
        [
            {
                "trade_date": requested_date,
                "market": "SZSE",
                "financing_balance_yuan": _number(row.get("jrrzye"), 100_000_000),
                "financing_buy_yuan": _number(row.get("jrrzmr"), 100_000_000),
                "securities_lending_balance_yuan": _number(row.get("jrrjye"), 100_000_000),
                "margin_total_balance_yuan": _number(row.get("jrrzrjye"), 100_000_000),
            }
            for row in rows
        ]
    )


def _expected_bse_margin(content: bytes, requested_date: str) -> pd.DataFrame:
    rows, selected_date = parse_bse_margin_summary(content.decode("utf-8"))
    if selected_date != requested_date:
        return pd.DataFrame()
    summary = next(
        (row for row in rows if str(row.get("zqdm") or "").strip() == "999999"),
        None,
    )
    if summary is None:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "trade_date": requested_date,
                "market": "BSE",
                "financing_balance_yuan": _number(summary.get("rzye")),
                "financing_buy_yuan": _number(summary.get("rzmre")),
                "securities_lending_balance_yuan": _number(summary.get("rqye")),
                "securities_lending_volume_shares": _number(summary.get("rqyl")),
                "securities_lending_sell_volume_shares": _number(summary.get("rqmcl")),
                "margin_total_balance_yuan": _number(summary.get("rzrqye")),
            }
        ]
    )


def _expected_hkex(content: bytes, requested_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    text = content.decode("utf-8").lstrip("\ufeff").strip()
    summary_rows: list[dict[str, Any]] = []
    top_rows: list[dict[str, Any]] = []
    for payload in parse_hkex_tab_data(text):
        if not payload.get("tradingDay"):
            continue
        label = str(payload.get("market", ""))
        market = "SSE" if label.startswith("SSE") else "SZSE"
        direction = "northbound" if "Northbound" in label else "southbound"
        currency = "RMB" if direction == "northbound" else "HKD"
        trade_date = payload.get("date") or requested_date
        for content_block in payload.get("content", []):
            table = content_block.get("table", {})
            schema = (table.get("schema") or [[]])[0]
            if content_block.get("style") == 1:
                values = [row.get("td", [[None]])[0][0] for row in table.get("tr", [])]
                raw = dict(zip(schema, values))
                buy = _number(raw.get("Buy Turnover"), 1_000_000)
                sell = _number(raw.get("Sell Turnover"), 1_000_000)
                summary_rows.append(
                    {
                        "trade_date": trade_date,
                        "market": market,
                        "direction": direction,
                        "currency": currency,
                        "total_turnover": _number(raw.get("Total Turnover"), 1_000_000),
                        "buy_turnover": buy,
                        "sell_turnover": sell,
                        "net_buy": None if buy is None or sell is None else buy - sell,
                        "total_trade_count": _number(raw.get("Total Trade Count")),
                        "etf_turnover": _number(raw.get("ETF Turnover"), 1_000_000),
                    }
                )
            elif content_block.get("style") == 2:
                for row in table.get("tr", []):
                    values = row.get("td", [[]])[0]
                    raw = dict(zip(schema, values))
                    instrument_code = (
                        str(raw.get("Stock Code", "")).zfill(6)
                        if direction == "northbound"
                        else str(raw.get("Stock Code", "")).zfill(5)
                    )
                    raw_name = clean_instrument_name(raw.get("Stock Name"))
                    instrument_name, instrument_name_source = (
                        resolve_hkex_instrument_name(
                            code=instrument_code,
                            direction=direction,
                            raw_name=raw_name,
                        )
                    )
                    top_rows.append(
                        {
                            "trade_date": trade_date,
                            "market": market,
                            "direction": direction,
                            "rank": None if _number(raw.get("Rank")) is None else int(_number(raw.get("Rank"))),
                                "instrument_code": instrument_code,
                                "instrument_name": instrument_name,
                                "instrument_name_raw": raw_name,
                                "instrument_name_source": instrument_name_source,
                            "buy_turnover": _number(raw.get("Buy Turnover")),
                            "sell_turnover": _number(raw.get("Sell Turnover")),
                            "total_turnover": _number(raw.get("Total Turnover")),
                        }
                    )
    return pd.DataFrame(summary_rows), pd.DataFrame(top_rows)


def validate_raw_to_normalized_mapping(
    manifest: dict[str, Any], workspace_root: Path
) -> list[QualityFinding]:
    """Independently reconcile source fields against every normalized row."""

    findings: list[QualityFinding] = []
    for artifact in manifest.get("artifacts") or []:
        source = artifact.get("source") or {}
        source_id = str(source.get("source_id", ""))
        dataset = str(source.get("dataset", ""))
        requested_date = str(artifact.get("requested_date") or "")
        label = f"{source_id}.{dataset}:{artifact.get('requested_date') or 'current'}"
        raw_path = workspace_root / str(artifact.get("raw_path", ""))
        normalized_paths = [workspace_root / path for path in _paths(artifact.get("normalized_path"))]
        result: dict[str, Any]
        try:
            content = raw_path.read_bytes()
            if (source_id, dataset) == ("sse", "etf_scale"):
                result = _compare_frames(
                    _expected_sse_etf(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "instrument_code"],
                    text_columns=["instrument_name"],
                    numeric_columns=["shares"],
                )
            elif (source_id, dataset) == ("sse", "etf_nav"):
                result = _compare_frames(
                    _expected_sse_etf_nav(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "instrument_code"],
                    text_columns=["instrument_name"],
                    numeric_columns=["nav"],
                )
            elif (source_id, dataset) == ("sse", "etf_pcf"):
                actual = pd.concat(
                    [_read_csv(path) for path in normalized_paths],
                    ignore_index=True,
                )
                expected = _expected_sse_etf_pcf(content, requested_date)
                for frame in (expected, actual):
                    if "listing_market" in frame.columns:
                        frame["listing_market"] = frame["listing_market"].fillna("").astype(str).str.strip()
                pcf_keys = ["trade_date", "market", "etf_code"]
                if "listing_market" in expected.columns or "listing_market" in actual.columns:
                    pcf_keys.append("listing_market")
                pcf_keys.append("stock_code")
                result = _compare_frames(
                    expected,
                    actual,
                    keys=pcf_keys,
                    text_columns=[
                        "etf_name",
                        "stock_name",
                        "listing_market",
                        "substitution_flag",
                        "source_id",
                        "source_grade",
                        "date_basis",
                    ],
                    numeric_columns=["quantity", "creation_unit"],
                )
            elif (source_id, dataset) == ("szse", "etf_scale"):
                expected_szse = (
                    _expected_szse_historical_etf(content, requested_date)
                    if source.get("date_semantics") == "source_historical_scale_size_date"
                    else _expected_szse_etf(content, requested_date)
                )
                result = _compare_frames(
                    expected_szse,
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "instrument_code"],
                    text_columns=["instrument_name"],
                    numeric_columns=["shares", "nav"],
                )
            elif (source_id, dataset) == ("sse", "margin_market_daily"):
                result = _compare_frames(
                    _expected_sse_margin(content),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market"],
                    text_columns=[],
                    numeric_columns=["financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan"],
                )
            elif (source_id, dataset) == ("sse", "margin_security_daily"):
                result = _compare_frames(
                    _expected_sse_margin_security(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "stock_code"],
                    text_columns=[
                        "source_trade_date",
                        "security_name",
                        "source_id",
                        "source_grade",
                        "date_basis",
                    ],
                    numeric_columns=[
                        "financing_balance_yuan",
                        "financing_buy_yuan",
                        "financing_repayment_yuan",
                        "securities_lending_balance_shares",
                        "securities_lending_sell_shares",
                        "securities_lending_repayment_shares",
                    ],
                )
            elif (source_id, dataset) == ("szse", "margin_market_daily"):
                result = _compare_frames(
                    _expected_szse_margin(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market"],
                    text_columns=[],
                    numeric_columns=["financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan"],
                )
            elif (source_id, dataset) == ("bse", "margin_market_daily"):
                result = _compare_frames(
                    _expected_bse_margin(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market"],
                    text_columns=[],
                    numeric_columns=["financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan"],
                )
            elif (source_id, dataset) == ("hkex", "stock_connect_daily"):
                expected_summary, expected_top = _expected_hkex(content, requested_date)
                actual_frames = [_read_csv(path) for path in normalized_paths]
                from .adapters.official import verified_hkex_closures
                closed = verified_hkex_closures(
                    {"requested_dates": [requested_date], "artifacts": [artifact]}, workspace_root)
                if requested_date in closed:
                    result = {"matched": len(actual_frames) == 2 and all(frame.empty for frame in actual_frames),
                              "official_closed": True, "raw_sha256": closed[requested_date]}
                else:
                    actual_summary = next(frame for frame in actual_frames if "rank" not in frame.columns)
                    actual_top = next(frame for frame in actual_frames if "rank" in frame.columns)
                    summary_result = _compare_frames(
                        expected_summary,
                        actual_summary,
                        keys=["trade_date", "market", "direction"],
                        text_columns=["currency"],
                        numeric_columns=["total_turnover", "buy_turnover", "sell_turnover", "net_buy", "total_trade_count", "etf_turnover"],
                    )
                    top_result = _compare_frames(
                        expected_top,
                        actual_top,
                        keys=["trade_date", "market", "direction", "rank"],
                        text_columns=["instrument_code", "instrument_name", "instrument_name_raw", "instrument_name_source"],
                        numeric_columns=["buy_turnover", "sell_turnover", "total_turnover"],
                    )
                    result = {"matched": summary_result["matched"] and top_result["matched"],
                              "summary": summary_result, "top10": top_result}
            else:
                result = {"matched": False, "error": "unsupported source dataset"}
        except (OSError, ValueError, KeyError, IndexError, StopIteration, TypeError) as exc:
            result = {"matched": False, "error": f"{type(exc).__name__}: {exc}"}
        findings.append(
            _finding(
                "mapping.raw_to_normalized_values",
                label,
                bool(result.get("matched")),
                message="原始字段与规范化键值逐行一致" if result.get("matched") else "原始字段与规范化结果不一致",
                evidence=result,
                remediation="阻断发布，核对字段名、单位倍率、日期和业务键；禁止按行号拼接。",
            )
        )
    return findings


def validate_artifact_lineage(
    manifest: dict[str, Any], workspace_root: Path
) -> list[QualityFinding]:
    """Validate that every manifest claim can be reproduced from files on disk."""

    findings: list[QualityFinding] = []
    artifacts = manifest.get("artifacts") or []
    findings.append(
        _finding(
            "lineage.artifacts_present",
            "manifest",
            bool(artifacts),
            message="采集证据清单非空" if artifacts else "采集证据清单为空",
            evidence={"artifact_count": len(artifacts)},
            remediation="阻断发布并重新执行官方数据采集。",
        )
    )

    normalized_owners: dict[str, list[str]] = {}
    observed_sources: set[str] = set()
    for index, artifact in enumerate(artifacts):
        source = artifact.get("source") or {}
        source_id = str(source.get("source_id", ""))
        dataset = str(source.get("dataset", ""))
        requested_date = artifact.get("requested_date") or "current"
        label = f"{source_id}.{dataset}:{requested_date}"
        observed_sources.add(source_id)

        parsed = urlparse(str(source.get("url", "")))
        approved = (
            parsed.scheme == "https"
            and parsed.hostname in APPROVED_SOURCE_HOSTS.get(source_id, set())
            and source.get("source_grade") == "A"
        )
        findings.append(
            _finding(
                "provenance.approved_official_source",
                label,
                approved,
                message="来源域名和来源等级可信" if approved else "来源不在官方白名单",
                evidence={
                    "source_id": source_id,
                    "url": source.get("url"),
                    "source_grade": source.get("source_grade"),
                },
                remediation="只允许 HTTPS 的上交所、深交所和港交所官方域名。",
            )
        )

        raw_path = workspace_root / str(artifact.get("raw_path", ""))
        metadata_path = workspace_root / str(artifact.get("metadata_path", ""))
        files_exist = raw_path.is_file() and metadata_path.is_file()
        findings.append(
            _finding(
                "lineage.raw_and_metadata_exist",
                label,
                files_exist,
                message="原始响应和元数据均存在" if files_exist else "原始证据文件缺失",
                evidence={
                    "raw_path": str(artifact.get("raw_path", "")),
                    "metadata_path": str(artifact.get("metadata_path", "")),
                },
                remediation="保留上一有效快照，重新采集缺失来源。",
            )
        )

        digest_matches = False
        metadata_matches = False
        if files_exist:
            digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            digest_matches = digest == artifact.get("sha256")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata_matches = all(
                    (
                        metadata.get("sha256") == artifact.get("sha256"),
                        metadata.get("status_code") == artifact.get("status_code"),
                        metadata.get("requested_date") == artifact.get("requested_date"),
                        (metadata.get("source") or {}).get("source_id") == source_id,
                        (metadata.get("source") or {}).get("dataset") == dataset,
                    )
                )
            except (OSError, ValueError, TypeError):
                metadata_matches = False
        findings.append(
            _finding(
                "lineage.sha256_matches",
                label,
                digest_matches,
                message="原始响应哈希可复验" if digest_matches else "原始响应哈希不一致",
                evidence={"expected_sha256": artifact.get("sha256")},
                remediation="将证据视为被修改或损坏，阻断发布并重新采集。",
            )
        )
        findings.append(
            _finding(
                "lineage.metadata_matches_manifest",
                label,
                metadata_matches,
                message="元数据与清单一致" if metadata_matches else "元数据与清单不一致",
                evidence={"artifact_index": index},
                remediation="检查请求日期、来源标识和清单写入流程。",
            )
        )

        normalized_paths = _paths(artifact.get("normalized_path"))
        normalized_rows = 0
        normalized_ok = bool(normalized_paths)
        for relative_path in normalized_paths:
            normalized_owners.setdefault(relative_path, []).append(label)
            path = workspace_root / relative_path
            if not path.is_file():
                normalized_ok = False
                continue
            try:
                normalized_rows += len(_read_csv(path))
            except (OSError, ValueError, pd.errors.ParserError):
                normalized_ok = False
        expected_rows = int(artifact.get("row_count") or 0)
        normalized_ok = normalized_ok and normalized_rows == expected_rows
        findings.append(
            _finding(
                "lineage.normalized_row_count",
                label,
                normalized_ok,
                message="规范化行数与解析结果一致" if normalized_ok else "规范化文件缺失、被覆盖或行数不符",
                evidence={
                    "expected_rows": expected_rows,
                    "observed_rows": normalized_rows,
                    "paths": normalized_paths,
                },
                remediation="为每个交易所使用独立存储命名空间，并重新采集本批数据。",
            )
        )

    collisions = {
        path: owners for path, owners in normalized_owners.items() if len(owners) > 1
    }
    findings.append(
        _finding(
            "lineage.normalized_paths_unique",
            "manifest",
            not collisions,
            message="各来源规范化路径互不覆盖" if not collisions else "不同来源复用了同一规范化路径",
            evidence={"collisions": collisions},
            remediation="将 source_id 纳入规范化文件目录，并重新采集。",
        )
    )
    required_sources = {"sse", "szse", "hkex"}
    if "bse" in observed_sources or "bse_margin" in (manifest.get("combined_paths") or {}):
        required_sources.add("bse")
    missing_sources = sorted(required_sources - observed_sources)
    findings.append(
        _finding(
            "provenance.required_publishers",
            "manifest",
            not missing_sources,
            message=("四家交易所来源齐全" if "bse" in required_sources else "三家交易所来源齐全") if not missing_sources else "缺少交易所来源",
            evidence={"observed": sorted(observed_sources), "missing": missing_sources},
            remediation="ETF、两融和互联互通任一官方来源缺失时不发布。",
        )
    )
    return findings


def _load_combined_frames(
    manifest: dict[str, Any], workspace_root: Path
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    expected_context = dict(EXPECTED_CONTEXT)
    if "bse_margin" in (manifest.get("combined_paths") or {}):
        expected_context["bse_margin"] = ("bse", {"BSE"})
    for name in expected_context:
        relative_path = (manifest.get("combined_paths") or {}).get(name)
        if not relative_path or not (workspace_root / relative_path).is_file():
            missing.append(name)
            frames[name] = pd.DataFrame()
            continue
        frames[name] = _read_csv(workspace_root / relative_path)
    return frames, missing


def validate_combined_data(
    manifest: dict[str, Any], frames: dict[str, pd.DataFrame], *,
    connect_closed_dates: dict[str, str] | None = None,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    requested_dates = sorted(str(value) for value in manifest.get("requested_dates", []))
    closed = set(connect_closed_dates or {}) & set(requested_dates)
    all_connect_closed = bool(requested_dates) and set(requested_dates).issubset(closed)

    artifact_counts: dict[tuple[str, str], int] = {}
    for artifact in manifest.get("artifacts") or []:
        source = artifact.get("source") or {}
        key = (str(source.get("source_id", "")), str(source.get("dataset", "")))
        artifact_counts[key] = artifact_counts.get(key, 0) + int(artifact.get("row_count") or 0)
    for source_key, combined_keys in COMBINED_DATASET_KEYS.items():
        expected = artifact_counts.get(source_key, 0)
        observed = sum(len(frames.get(key, pd.DataFrame())) for key in combined_keys)
        findings.append(
            _finding(
                "lineage.combined_row_count",
                ".".join(source_key),
                expected == observed and (expected > 0 or (source_key == ("hkex", "stock_connect_daily") and all_connect_closed)),
                message="合并明细未丢行" if expected == observed and expected > 0 else "合并明细行数与单源解析不一致",
                evidence={
                    "expected_rows": expected,
                    "observed_rows": observed,
                    "combined_datasets": list(combined_keys),
                },
                remediation="阻断发布，检查合并键、空表处理和跨来源覆盖。",
            )
        )

    required_columns = {
        "sse_etf": ["trade_date", "market", "instrument_code", "instrument_name", "shares", "source_id", "source_grade"],
        "szse_etf": ["trade_date", "market", "instrument_code", "instrument_name", "shares", "source_id", "source_grade"],
        "sse_margin": ["trade_date", "market", "financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan", "source_id"],
        "szse_margin": ["trade_date", "market", "financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan", "source_id"],
        "bse_margin": ["trade_date", "market", "financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan", "source_id"],
        "stock_connect_summary": ["trade_date", "market", "direction", "currency", "total_turnover", "total_trade_count", "etf_turnover", "source_id"],
        "stock_connect_top": ["trade_date", "market", "direction", "rank", "instrument_code", "instrument_name", "total_turnover", "source_id"],
    }
    # Names are descriptive metadata for HKEX's top-10 table.  The official
    # file can omit one while the code, rank and turnover remain valid, so do
    # not make a missing name a hard release blocker.
    required_values = {
        **required_columns,
        "stock_connect_top": [
            "trade_date",
            "market",
            "direction",
            "rank",
            "instrument_code",
            "total_turnover",
            "source_id",
        ],
    }
    expected_context = dict(EXPECTED_CONTEXT)
    if "bse_margin" in frames:
        expected_context["bse_margin"] = ("bse", {"BSE"})
    for name, (source_id, markets) in expected_context.items():
        frame = frames.get(name, pd.DataFrame())
        if name in {"stock_connect_summary", "stock_connect_top"} and all_connect_closed and frame.empty:
            findings.append(_finding("mapping.official_closed", name, True,
                message="官方原件确认四通道休市；无数值行", evidence={"closed_dates": sorted(closed)}))
            continue
        missing_columns = sorted(set(required_columns[name]) - set(frame.columns))
        null_rows = 0 if missing_columns else int(frame[required_values[name]].isna().any(axis=1).sum())
        invalid_context = len(frame)
        if not frame.empty and {"source_id", "market"}.issubset(frame.columns):
            invalid_context = int(
                ((frame["source_id"].astype(str) != source_id) | (~frame["market"].isin(markets))).sum()
            )
        findings.append(
            _finding(
                "mapping.required_values",
                name,
                not missing_columns and null_rows == 0,
                message="匹配键和核心数值完整" if not missing_columns and null_rows == 0 else "匹配键或核心数值缺失",
                evidence={"missing_columns": missing_columns, "rows_with_nulls": null_rows},
                remediation="不要用行号拼接；以日期、市场、代码等业务键重新映射。",
            )
        )
        if name == "stock_connect_top" and not frame.empty:
            name_series = frame.get("instrument_name", pd.Series(dtype="string"))
            normalized_names = name_series.astype("string").str.strip()
            unresolved = int(normalized_names.isna().sum() + normalized_names.eq("").sum())
            override_rows = int(
                frame.get("instrument_name_source", pd.Series(dtype="string"))
                .astype(str)
                .eq("override")
                .sum()
            )
            findings.append(
                _finding(
                    "mapping.instrument_name_resolution",
                    name,
                    unresolved == 0,
                    message="证券名称已解析" if unresolved == 0 else "部分证券名称尚未解析",
                    evidence={
                        "unresolved_rows": unresolved,
                        "override_rows": override_rows,
                        "rows": len(frame),
                    },
                    severity="medium",
                    warn_only=True,
                    remediation="保留代码并显示“名称待确认”；确认官方名称后只更新异常兜底表，不修改原始文件。",
                )
            )
        findings.append(
            _finding(
                "mapping.source_market_consistency",
                name,
                invalid_context == 0 and not frame.empty,
                message="来源与市场映射一致" if invalid_context == 0 and not frame.empty else "来源与市场映射错位",
                evidence={"invalid_rows": invalid_context, "expected_source": source_id, "expected_markets": sorted(markets)},
                remediation="阻断发布并检查适配器常量和跨市场合并。",
            )
        )

    sse_etf = frames.get("sse_etf", pd.DataFrame())
    counts = (
        sse_etf.groupby("trade_date")["instrument_code"].nunique().astype(int).to_dict()
        if {"trade_date", "instrument_code"}.issubset(sse_etf.columns)
        else {}
    )
    ratio = min(counts.values()) / max(counts.values()) if counts and max(counts.values()) else 0.0
    findings.append(
        _finding(
            "volume.sse_etf_daily_stability",
            "sse_etf",
            set(counts) == set(requested_dates) and ratio >= 0.8,
            message="ETF 日覆盖量稳定" if set(counts) == set(requested_dates) and ratio >= 0.8 else "ETF 日覆盖量异常跳变",
            evidence={"daily_unique_codes": counts, "min_max_ratio": round(ratio, 4)},
            severity="high",
            remediation="检查分页、交易日和源站空响应；不要发布断崖式缺行快照。",
        )
    )

    connect = frames.get("stock_connect_summary", pd.DataFrame())
    expected_channels = {("SSE", "northbound"), ("SSE", "southbound"), ("SZSE", "northbound"), ("SZSE", "southbound")}
    invalid_channel_dates: dict[str, list[str]] = {}
    if {"trade_date", "market", "direction", "currency"}.issubset(connect.columns):
        for trade_date, group in connect.groupby("trade_date"):
            channels = set(zip(group["market"].astype(str), group["direction"].astype(str)))
            currency_ok = bool(
                ((group["direction"].eq("northbound") & group["currency"].eq("RMB"))
                 | (group["direction"].eq("southbound") & group["currency"].eq("HKD"))).all()
            )
            if channels != expected_channels or not currency_ok:
                invalid_channel_dates[str(trade_date)] = [f"{market}:{direction}" for market, direction in sorted(channels)]
    observed_connect_dates = set(connect.get("trade_date", pd.Series(dtype=str)).astype(str).unique())
    channels_ok = (observed_connect_dates | closed) == set(requested_dates) and not invalid_channel_dates and not (observed_connect_dates & closed)
    findings.append(
        _finding(
            "mapping.connect_channels",
            "stock_connect_summary",
            channels_ok,
            message="每日四通道和币种映射完整" if channels_ok else "互联互通通道或币种映射异常",
            evidence={"invalid_dates": invalid_channel_dates, "expected_dates": requested_dates},
            remediation="按 market + direction 业务键核对四通道，不以页面顺序匹配。",
        )
    )
    return findings


def _all_numbers_finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, dict):
        return all(_all_numbers_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_numbers_finite(item) for item in value)
    return True


ETF_PERIOD_WINDOWS: dict[str, int] = {
    "1d": 1,
    "3d": 3,
    "1w": 5,
    "2w": 10,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "12m": 252,
}


def validate_etf_terminal_data(snapshot: dict[str, Any]) -> list[QualityFinding]:
    """Validate the derived ETF terminal, not only its raw input files.

    Raw-to-normalized checks cannot detect a partial rolling window after the
    data has been pivoted.  A terminal value is therefore publishable only if
    its declared coverage equals the requested number of adjacent official
    daily changes.  This is deliberately a hard gate: a partial value is less
    useful than an explicit ``null``/``覆盖不足`` state for a decision product.
    """

    terminal = snapshot.get("etfTerminal")
    if not isinstance(terminal, dict):
        return [
            _finding(
                "derived.etf_terminal_shape",
                "dashboard_snapshot",
                False,
                message="ETF 派生终端缺失，无法核验窗口覆盖",
                evidence={},
                remediation="先生成包含官方 ETF 份额窗口的终端，再进入发布门禁。",
            )
        ]

    instruments = terminal.get("instruments")
    if not isinstance(instruments, list):
        instruments = []
    duplicate_keys: list[str] = []
    seen: set[str] = set()
    coverage_issues: list[dict[str, Any]] = []
    cash_issues: list[dict[str, Any]] = []
    for index, item in enumerate(instruments):
        if not isinstance(item, dict):
            coverage_issues.append({"index": index, "reason": "invalid_instrument"})
            continue
        code = str(item.get("code") or "")
        market = str(item.get("market") or "SSE")
        key = f"{market}:{code}"
        if key in seen:
            duplicate_keys.append(key)
        seen.add(key)
        periods = item.get("periods") if isinstance(item.get("periods"), dict) else {}
        period_coverage = item.get("periodCoverage") if isinstance(item.get("periodCoverage"), dict) else {}
        cash_periods = item.get("estimatedCashPeriods") if isinstance(item.get("estimatedCashPeriods"), dict) else {}
        cash_coverage = item.get("cashPeriodCoverage") if isinstance(item.get("cashPeriodCoverage"), dict) else {}
        available_periods = terminal.get("availablePeriods") if isinstance(terminal.get("availablePeriods"), list) else list(periods)
        for period in available_periods:
            if not isinstance(period, str):
                continue
            expected = ETF_PERIOD_WINDOWS.get(period)
            coverage = period_coverage.get(period)
            value = periods.get(period)
            if expected is not None:
                if not isinstance(coverage, int) or coverage < 0 or coverage > expected:
                    coverage_issues.append({"key": key, "period": period, "coverage": coverage, "expected": expected})
                elif value is not None and coverage != expected:
                    coverage_issues.append({"key": key, "period": period, "coverage": coverage, "expected": expected, "reason": "partial_value"})
                elif value is None and coverage == expected:
                    coverage_issues.append({"key": key, "period": period, "coverage": coverage, "expected": expected, "reason": "missing_value"})
            cash_value = cash_periods.get(period)
            cash_count = cash_coverage.get(period)
            if cash_value is not None and expected is not None:
                if not isinstance(cash_count, int) or cash_count != expected:
                    cash_issues.append({"key": key, "period": period, "coverage": cash_count, "expected": expected})

    findings = [
        _finding(
            "derived.etf_identity_unique",
            "etfTerminal.instruments",
            not duplicate_keys,
            message="ETF 代码与市场组合键唯一" if not duplicate_keys else "ETF 代码与市场组合键重复",
            evidence={"duplicate_keys": duplicate_keys[:20], "instrument_count": len(instruments)},
            remediation="以 market + code 去重，保留原始发行市场，不按名称或页面行号合并。",
        ),
        _finding(
            "derived.etf_period_coverage",
            "etfTerminal.instruments",
            not coverage_issues,
            message="ETF 窗口只发布完整官方日点" if not coverage_issues else "ETF 窗口存在部分或错配日点",
            evidence={"issues": coverage_issues[:50], "issue_count": len(coverage_issues)},
            remediation="缺少任一官方日点时返回 null/覆盖不足；禁止用最新可用点补成 1d/3d/1w。",
        ),
        _finding(
            "derived.etf_cash_period_coverage",
            "etfTerminal.instruments",
            not cash_issues,
            message="ETF 金额估算只使用完整净值覆盖" if not cash_issues else "ETF 金额估算存在部分净值覆盖",
            evidence={"issues": cash_issues[:50], "issue_count": len(cash_issues)},
            severity="high",
            remediation="份额或对应交易日 NAV 缺失时不估算金额，不用当前 NAV 代替历史 NAV。",
        ),
    ]
    groups = terminal.get("groupPeriods") if isinstance(terminal.get("groupPeriods"), list) else []
    group_issues: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            group_issues.append({"reason": "invalid_group"})
            continue
        group_periods = group.get("periods") if isinstance(group.get("periods"), dict) else {}
        comparable = group.get("comparableCount") if isinstance(group.get("comparableCount"), dict) else {}
        for period, value in group_periods.items():
            if value is not None and int(comparable.get(period, 0) or 0) <= 0:
                group_issues.append({"group": group.get("name"), "period": period, "reason": "value_without_components"})
    findings.append(
        _finding(
            "derived.etf_group_reconciliation",
            "etfTerminal.groupPeriods",
            not group_issues,
            message="板块汇总均有可追溯 ETF 组成" if not group_issues else "板块汇总缺少可追溯组成",
            evidence={"issues": group_issues[:50], "issue_count": len(group_issues)},
            remediation="板块只汇总通过完整窗口校验的 ETF，并保留可比数量。",
        )
    )
    return findings


def validate_etf_terminal_identity(
    snapshot: dict[str, Any], frames: dict[str, pd.DataFrame], as_of: str
) -> list[QualityFinding]:
    """Reconcile displayed ETF identity and latest shares to raw rows."""

    terminal = snapshot.get("etfTerminal")
    instruments = terminal.get("instruments") if isinstance(terminal, dict) else None
    if not isinstance(instruments, list):
        return []
    parts: list[pd.DataFrame] = []
    for key, market in (("sse_etf", "SSE"), ("szse_etf", "SZSE")):
        frame = frames.get(key, pd.DataFrame())
        if frame.empty or not {"trade_date", "instrument_code", "instrument_name", "shares"}.issubset(frame.columns):
            continue
        selected = frame.loc[frame["trade_date"].astype(str).eq(as_of)].copy()
        if not selected.empty:
            selected["market"] = market
            parts.append(selected[["market", "instrument_code", "instrument_name", "shares"]])
    if not parts:
        return [
            _finding(
                "derived.etf_identity_matches_raw",
                "etfTerminal.instruments",
                False,
                message="无法找到用于核对 ETF 展示身份的原始日点",
                evidence={"as_of": as_of},
                remediation="缺少目标日原始 ETF 行时保持 pending，不从其他日期或名称表补值。",
            )
        ]
    raw = pd.concat(parts, ignore_index=True)
    raw["instrument_code"] = raw["instrument_code"].astype(str).str.zfill(6)
    raw["instrument_name"] = raw["instrument_name"].astype(str).str.strip()
    raw["shares"] = pd.to_numeric(raw["shares"], errors="coerce")
    duplicate_conflicts = (
        raw.groupby(["market", "instrument_code"])
        .agg(names=("instrument_name", "nunique"), shares=("shares", "nunique"))
        .reset_index()
    )
    conflict_keys = [
        f"{row.market}:{row.instrument_code}"
        for row in duplicate_conflicts.itertuples()
        if int(row.names) > 1 or int(row.shares) > 1
    ]
    raw_latest = raw.drop_duplicates(["market", "instrument_code"], keep="last").set_index(["market", "instrument_code"])
    identity_issues: list[dict[str, Any]] = []
    share_issues: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        market = str(item.get("market") or "SSE")
        code = str(item.get("code") or "").zfill(6)
        key = (market, code)
        if key not in raw_latest.index:
            identity_issues.append({"key": f"{market}:{code}", "reason": "missing_raw_row"})
            continue
        source = raw_latest.loc[key]
        source_name = str(source["instrument_name"])
        if str(item.get("name") or "").strip() != source_name:
            identity_issues.append({"key": f"{market}:{code}", "display": item.get("name"), "raw": source_name})
        displayed_shares = item.get("shares")
        if displayed_shares is not None and pd.notna(source["shares"]):
            expected_shares = float(source["shares"]) / 100_000_000
            try:
                if abs(float(displayed_shares) - expected_shares) > 0.01:
                    share_issues.append({"key": f"{market}:{code}", "display": displayed_shares, "raw_100m": round(expected_shares, 6)})
            except (TypeError, ValueError):
                share_issues.append({"key": f"{market}:{code}", "display": displayed_shares, "raw_100m": round(expected_shares, 6)})
    return [
        _finding(
            "derived.etf_identity_matches_raw",
            "etfTerminal.instruments",
            not identity_issues and not conflict_keys,
            message="ETF 名称、代码和市场与目标日原始行一致" if not identity_issues and not conflict_keys else "ETF 展示身份与原始行不一致",
            evidence={"issues": identity_issues[:50], "conflict_keys": conflict_keys[:50], "issue_count": len(identity_issues)},
            remediation="以目标日原始行核对 market+code+name；冲突时保留原始证据并阻断发布。",
        ),
        _finding(
            "derived.etf_shares_matches_raw",
            "etfTerminal.instruments",
            not share_issues,
            message="ETF 展示份额与原始份额单位换算一致" if not share_issues else "ETF 展示份额与原始份额不一致",
            evidence={"issues": share_issues[:50], "issue_count": len(share_issues), "unit": "100m shares"},
            remediation="按原始份额/100,000,000 复算，禁止用规模或其他市场同代码数据替代。",
        ),
    ]


def _canonical_etf_delta(
    frames: dict[str, pd.DataFrame],
    *,
    dates: list[str],
    current_date: str,
    allowed_keys: set[tuple[str, str]] | None = None,
) -> tuple[float, dict[str, Any]]:
    """Return a raw integer-share delta keyed by market and ETF code."""

    canonical = build_canonical_etf_daily_fact(
        frames.get("sse_etf", pd.DataFrame()),
        frames.get("szse_etf", pd.DataFrame()),
        dates,
    )
    requested = sorted({str(value) for value in dates if str(value) <= current_date})
    available = set(
        str(value)
        for value in canonical.get("trade_date", pd.Series(dtype=str)).dropna().unique()
    )
    if len(requested) < 2 or current_date not in available or requested[-2] not in available:
        raise ValueError("canonical_etf_previous_date_missing")
    previous_date = requested[-2]
    latest = canonical.loc[
        canonical["trade_date"].eq(current_date),
        ["market", "instrument_code", "shares"],
    ].rename(columns={"shares": "latest_shares"})
    previous = canonical.loc[
        canonical["trade_date"].eq(previous_date),
        ["market", "instrument_code", "shares"],
    ].rename(columns={"shares": "previous_shares"})
    comparable = latest.merge(previous, on=["market", "instrument_code"], how="inner")
    if allowed_keys is not None:
        comparable = comparable.loc[
            comparable.apply(
                lambda row: (str(row["market"]), str(row["instrument_code"])) in allowed_keys,
                axis=1,
            )
        ]
    comparable["delta"] = comparable["latest_shares"] - comparable["previous_shares"]
    return (
        float(comparable["delta"].sum()) / 100_000_000,
        {
            "current_date": current_date,
            "previous_date": previous_date,
            "comparable_count": int(len(comparable)),
            "raw_share_delta": int(comparable["delta"].sum()),
        },
    )


def validate_etf_cross_artifact_reconciliation(
    snapshot: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    *,
    dates: list[str],
    as_of: str,
) -> list[QualityFinding]:
    """Check canonical raw shares against every ETF-facing read-model view."""

    terminal = snapshot.get("etfTerminal") if isinstance(snapshot.get("etfTerminal"), dict) else {}
    terminal_items = [item for item in terminal.get("instruments", []) if isinstance(item, dict)]
    allowed_keys = {
        (str(item.get("market") or "SSE"), str(item.get("code") or "").zfill(6))
        for item in terminal_items
        if isinstance(item.get("periods"), dict)
        and isinstance(item["periods"].get("1d"), (int, float))
    }
    tolerance = 0.01
    evidence: dict[str, Any] = {"tolerance_100m_shares": tolerance, "as_of": as_of}
    market_totals: dict[str, float] = {}
    declared_universe: dict[str, Any] = {}
    classification_counts: dict[str, int] = {"classified": 0, "unclassified": 0, "total": len(terminal_items)}
    try:
        raw_total, raw_meta = _canonical_etf_delta(
            frames,
            dates=dates,
            current_date=as_of,
            allowed_keys=allowed_keys,
        )
        radar_total = sum(
            float(item["periods"]["1d"])
            for item in terminal_items
            if isinstance(item.get("periods"), dict) and isinstance(item["periods"].get("1d"), (int, float))
        )
        for item in terminal_items:
            value = item.get("periods", {}).get("1d") if isinstance(item.get("periods"), dict) else None
            if isinstance(value, (int, float)):
                market = str(item.get("market") or "SSE")
                market_totals[market] = market_totals.get(market, 0.0) + float(value)
            bucket = "unclassified" if item.get("category") == "未归类" else "classified"
            classification_counts[bucket] += 1
        declared_universe = terminal.get("universe") if isinstance(terminal.get("universe"), dict) else {}
        rotation_total = sum(
            float(group["periods"]["1d"])
            for group in terminal.get("groupPeriods", [])
            if isinstance(group, dict)
            and isinstance(group.get("periods"), dict)
            and isinstance(group["periods"].get("1d"), (int, float))
        )
        views = build_view_payloads(snapshot)
        bootstrap_reconciliation = (
            views["bootstrap"].get("etfTerminal", {}).get("reconciliation", {}).get("1d", {}).get("shares", {})
        )
        bootstrap_instrument_total = float(bootstrap_reconciliation.get("instrumentTotal"))
        bootstrap_group_total = float(bootstrap_reconciliation.get("groupTotal"))
        actuals = {
            "canonicalRaw": raw_total,
            "radarInstrumentTotal": radar_total,
            "rotationGroupTotal": rotation_total,
            "bootstrapInstrumentTotal": bootstrap_instrument_total,
            "bootstrapGroupTotal": bootstrap_group_total,
        }
        evidence.update({"raw": raw_meta, "totals": actuals, "marketTotals": market_totals, "classificationCounts": classification_counts})
        passed = all(abs(value - raw_total) <= tolerance for value in actuals.values())
    except (KeyError, TypeError, ValueError, ImportError):
        passed = False
        evidence["error"] = "canonical ETF cross-artifact reconciliation could not be computed"

    market_passed = (
        bool(market_totals)
        and abs(sum(market_totals.values()) - (evidence.get("totals", {}).get("radarInstrumentTotal", 0.0))) <= tolerance
        and {"SSE", "SZSE"}.issuperset(market_totals.keys())
    )
    declared_classified = declared_universe.get("classified")
    declared_unclassified = declared_universe.get("unclassified")
    declared_total = declared_universe.get("total")
    classification_passed = (
        classification_counts["classified"] + classification_counts["unclassified"] == classification_counts["total"]
        and declared_total == classification_counts["total"]
        and declared_classified == classification_counts["classified"]
        and declared_unclassified == classification_counts["unclassified"]
    )
    evidence["declaredUniverse"] = {
        "total": declared_total,
        "classified": declared_classified,
        "unclassified": declared_unclassified,
    }
    return [
        _finding(
            "reconciliation.etf_cross_artifact_identity",
            "dashboard_read_models",
            passed,
            message="总览、ETF 雷达、板块轮动与 canonical ETF 日事实一致" if passed else "ETF 视图之间存在资金流口径差异",
            evidence=evidence,
            remediation="所有 ETF 视图必须从 market+code+trade_date canonical fact 派生；先修复原始键、日期或单位，再发布。",
        ),
        _finding(
            "reconciliation.etf_market_identity",
            "etfTerminal.instruments",
            market_passed,
            message="沪市、深市与全市场 ETF 份额变化可加总" if market_passed else "ETF 市场分项与全市场合计不一致",
            evidence=evidence,
            remediation="保留 market 维度，按 market+code 对账；禁止把同代码跨市场产品合并为一行。",
        ),
        _finding(
            "reconciliation.etf_classification_identity",
            "etfTerminal.universe",
            classification_passed,
            message="已归类与未归类 ETF 覆盖全部合资格产品" if classification_passed else "ETF 分类计数与合资格产品总数不一致",
            evidence=evidence,
            remediation="每个合资格 ETF 必须恰好进入一个分类桶，低置信度产品进入未归类，不得静默丢弃。",
        ),
    ]


def _metric_value(snapshot: dict[str, Any], metric_id: str) -> float:
    metric = next(item for item in snapshot.get("metrics", []) if item.get("id") == metric_id)
    return float(str(metric["value"]).replace(",", ""))


def _future_only_failure(finding: QualityFinding | dict[str, Any], published_as_of: str) -> bool:
    """Whether a failure is exclusively about a date later than the snapshot.

    Official sources publish at different times.  A later requested date stays
    visible as pending, but must not prevent QA from approving an earlier,
    independently complete snapshot.
    """

    dataset = str(
        finding.dataset if isinstance(finding, QualityFinding) else finding.get("dataset", "")
    )
    evidence = finding.evidence if isinstance(finding, QualityFinding) else finding.get("evidence") or {}
    label_date = re.search(r":(20\d{2}-\d{2}-\d{2})$", dataset)
    if label_date and label_date.group(1) > published_as_of:
        return True
    missing_dates = evidence.get("missing_dates") if isinstance(evidence, dict) else None
    return bool(missing_dates) and all(str(date) > published_as_of for date in missing_dates)


def _pending_future_warning(finding: QualityFinding) -> QualityFinding:
    return QualityFinding(
        check_id=finding.check_id,
        dataset=finding.dataset,
        status="warn",
        severity="high",
        message=f"{finding.message}（晚于已发布快照，保留为待补）",
        evidence=finding.evidence,
        remediation=finding.remediation,
    )


def _quality_allows_published_snapshot(quality: dict[str, Any], published_as_of: str) -> bool:
    failures = [
        finding
        for finding in quality.get("findings", [])
        if finding.get("status") == "fail"
    ]
    return bool(failures) and all(
        _future_only_failure(finding, published_as_of) for finding in failures
    )


def _with_archived_etf_inputs(manifest, frames, workspace_root, as_of):
    """Derived QA consumes the builder's verified adjacent input, not just current CSVs.

    Keep collected frames unchanged for their own row-count/raw reconciliation.
    The archived baseline is separately hash/date/market checked by the shared
    reader, and is used only for derived ETF identity and delta comparisons.
    """
    from .etf_archive_adjacent import archived_inputs
    derived = dict(frames)
    # Identity is pinned to the original collection window even when the
    # selected snapshot is an older complete-day fallback. Projection to the
    # selected date never changes that reference or pulls future facts back.
    anchor = max(manifest.get("requested_dates") or [as_of])
    for _reference, frame in archived_inputs(manifest, workspace_root, anchor):
        frame = frame[frame["trade_date"].astype(str).le(as_of)]
        derived["szse_etf"] = pd.concat([derived.get("szse_etf", pd.DataFrame()), frame], ignore_index=True, sort=False)
    return derived


def validate_snapshot_values(
    manifest: dict[str, Any],
    quality: dict[str, Any],
    snapshot: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    *,
    manifest_path: Path,
    quality_path: Path,
    workspace_root: Path,
    state_path: Path | None = None,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    dates = sorted(str(value) for value in manifest.get("requested_dates", []))
    as_of = str(snapshot.get("asOf") or (dates[-1] if dates else ""))
    frames = _with_archived_etf_inputs(manifest, frames, workspace_root, as_of)
    published_dates = [trade_date for trade_date in dates if trade_date <= as_of]
    expected_snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=workspace_root,
    )
    if state_path is not None:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_snapshot = materialize_dashboard_snapshot(
            expected_snapshot,
            state,
            as_of=as_of,
        )
    exact_match = snapshot == expected_snapshot
    findings.append(
        _finding(
            "publication.snapshot_reproducible",
            "dashboard_snapshot",
            exact_match,
            message="发布快照可由已验证明细逐字节重建" if exact_match else "发布快照与明细重建结果不一致",
        evidence={"as_of": snapshot.get("asOf"), "expected_as_of": as_of},
            remediation="重新生成快照，禁止手工修改发布 JSON。",
        )
    )
    findings.append(
        _finding(
            "publication.snapshot_finite",
            "dashboard_snapshot",
            _all_numbers_finite(snapshot),
            message="页面快照不含 NaN 或无穷值" if _all_numbers_finite(snapshot) else "页面快照包含非有限数值",
            evidence={},
            remediation="在计算层处理缺失值，不允许 NaN/Infinity 进入 JSON。",
        )
    )
    findings.extend(validate_etf_terminal_data(snapshot))
    findings.extend(validate_etf_terminal_identity(snapshot, frames, as_of))
    findings.extend(
        validate_etf_cross_artifact_reconciliation(
            snapshot,
            frames,
            dates=published_dates,
            as_of=as_of,
        )
    )

    headline_ok = False
    evidence: dict[str, Any] = {}
    try:
        terminal = snapshot.get("etfTerminal") if isinstance(snapshot.get("etfTerminal"), dict) else {}
        allowed_keys = {
            (str(item.get("market") or "SSE"), str(item.get("code") or "").zfill(6))
            for item in terminal.get("instruments", [])
            if isinstance(item, dict)
            and isinstance(item.get("periods"), dict)
            and isinstance(item["periods"].get("1d"), (int, float))
        }
        etf_expected, etf_raw_meta = _canonical_etf_delta(
            frames,
            dates=published_dates,
            current_date=as_of,
            allowed_keys=allowed_keys,
        )
        etf_expected = round(etf_expected, 2)

        margin_frames = [frames["sse_margin"], frames["szse_margin"]]
        if "bse_margin" in frames and not frames["bse_margin"].empty:
            margin_frames.append(frames["bse_margin"])
        margin = pd.concat(margin_frames, ignore_index=True)
        margin_expected = round(float(margin[margin["trade_date"].astype(str).eq(as_of)]["financing_balance_yuan"].sum()) / 1_000_000_000_000, 4)
        connect = frames["stock_connect_summary"]
        if connect.empty:
            connect = connect.reindex(columns=["trade_date", "direction", "net_buy", "total_turnover"])
        current = connect[connect["trade_date"].astype(str).eq(as_of)]
        from .adapters.official import verified_hkex_closures
        connect_closed = as_of in verified_hkex_closures(manifest, workspace_root)
        south_expected = None if connect_closed else round(float(current[current["direction"].eq("southbound")]["net_buy"].sum()) / 100_000_000, 2)
        north_expected = None if connect_closed else round(float(current[current["direction"].eq("northbound")]["total_turnover"].sum()) / 100_000_000, 2)
        def connect_metric(metric_id):
            if connect_closed:
                metric = next(item for item in snapshot.get("metrics", []) if item.get("id") == metric_id)
                if metric.get("value") != "—" or not current.empty:
                    raise ValueError("closed_connect_has_numeric_value")
                return None
            return _metric_value(snapshot, metric_id)
        actual = {
            "etf": _metric_value(snapshot, "etf"),
            "southbound": connect_metric("southbound"),
            "margin": _metric_value(snapshot, "margin"),
            "northbound": connect_metric("northbound"),
        }
        expected = {
            "etf": etf_expected,
            "southbound": south_expected,
            "margin": margin_expected,
            "northbound": north_expected,
        }
        tolerances = {"etf": 0.01, "southbound": 0.01, "margin": 0.0001, "northbound": 0.01}
        headline_ok = all(actual[key] is None if expected[key] is None else
                          actual[key] is not None and abs(actual[key] - expected[key]) <= tolerances[key]
                          for key in expected)
        evidence = {
            "expected": expected,
            "actual": actual,
            "tolerances": tolerances,
            "etf_raw": etf_raw_meta,
        }
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        headline_ok = False
        evidence = {"error": "headline reconciliation could not be computed"}
    findings.append(
        _finding(
            "reconciliation.dashboard_headlines",
            "dashboard_snapshot",
            headline_ok,
            message="四个首页指标可由明细独立反算" if headline_ok else "首页指标无法与明细对账",
            evidence=evidence,
            remediation="阻断发布，核对日期、单位和汇总公式。",
        )
    )

    base_gate_ok = (
        snapshot.get("publication", {}).get("status") == "confirmed"
        and _quality_allows_published_snapshot(quality, as_of)
    ) or (
        int((quality.get("counts") or {}).get("fail", 0)) == 0
        and quality.get("phase0_gate_status") != "blocked"
    )
    findings.append(
        _finding(
            "publication.base_quality_gate",
            "quality_summary",
            base_gate_ok,
            message="基础质量门禁允许发布" if base_gate_ok else "基础质量门禁已阻断",
            evidence={"counts": quality.get("counts"), "gate": quality.get("phase0_gate_status")},
            remediation="先修复基础质量失败项，再运行发布 QA。",
        )
    )
    return findings


def run_release_qa(
    *,
    manifest_path: Path,
    quality_path: Path,
    snapshot_path: Path,
    output_path: Path,
    workspace_root: Path,
    state_path: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    findings: list[QualityFinding] = []
    findings.extend(validate_artifact_lineage(manifest, workspace_root))
    findings.extend(validate_raw_to_normalized_mapping(manifest, workspace_root))
    frames, missing = _load_combined_frames(manifest, workspace_root)
    findings.append(
        _finding(
            "lineage.combined_files_exist",
            "manifest",
            not missing,
            message="全部合并明细存在" if not missing else "合并明细缺失",
            evidence={"missing": missing},
            remediation="不发布缺失任一数据域的快照。",
        )
    )
    from .adapters.official import verified_hkex_closures
    findings.extend(validate_combined_data(manifest, frames,
        connect_closed_dates=verified_hkex_closures(manifest, workspace_root)))
    if not missing:
        findings.extend(
            validate_snapshot_values(
                manifest,
                quality,
                snapshot,
                frames,
                manifest_path=manifest_path,
                quality_path=quality_path,
                workspace_root=workspace_root,
                state_path=state_path,
            )
        )

    published_as_of = str(snapshot.get("asOf") or "")
    if snapshot.get("publication", {}).get("status") == "confirmed" and published_as_of:
        findings = [
            _pending_future_warning(finding)
            if finding.status == "fail" and _future_only_failure(finding, published_as_of)
            else finding
            for finding in findings
        ]

    summary = summarize_findings(findings)
    summary.update(
        {
            "run_id": manifest.get("run_id"),
            "generated_at": now_iso(),
            "verdict": "pass" if summary["counts"]["fail"] == 0 else "blocked",
            "known_limitations": [
                "深交所 ETF 文件按列表页披露日归属到前一已知交易日。",
                "ETF 默认展示份额变化；金额口径仅在对应交易日官方单位净值覆盖时显示，仍是估算值而非精确申赎清算金额。",
                "北向自披露调整后不提供买入额、卖出额或净流入，页面只展示官方成交总额。",
            ],
        }
    )
    write_json(output_path, summary)
    return summary


def failed_findings(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return (item for item in report.get("findings", []) if item.get("status") == "fail")
