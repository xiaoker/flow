#!/usr/bin/env python3
"""Build one deterministic SSE PCF v2 capture plan from ETF share snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from scripts.capture_sse_pcf_v2 import fetch_xml
from trading_desk.pcf_capture import build_changed_etf_capture_plan
from trading_desk.pcf_xml import parse_sse_etf_pcf_xml


ALIASES = {
    "SEC_CODE": "instrument_code",
    "基金代码": "instrument_code",
    "SEC_NAME": "instrument_name",
    "基金简称": "instrument_name",
    "TOT_VOL": "shares",
    "当前规模(份)": "shares",
}

SSE_SCALE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SCALE_REFERER = "https://www.sse.com.cn/assortment/fund/etf/list/scale/"


def normalise_sse_shares(frame: pd.DataFrame) -> pd.DataFrame:
    value = frame.rename(columns={key: target for key, target in ALIASES.items() if key in frame.columns}).copy()
    missing = sorted({"trade_date", "instrument_code", "shares"} - set(value.columns))
    if missing:
        raise ValueError(f"shares_missing_columns:{','.join(missing)}")
    if "market" not in value.columns:
        value["market"] = "SSE"
    if "instrument_name" not in value.columns:
        value["instrument_name"] = value["instrument_code"].astype(str)
    value["trade_date"] = value["trade_date"].astype(str).str[:10]
    value["instrument_code"] = (
        value["instrument_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    value["market"] = value["market"].astype(str).str.upper()
    return value.loc[
        value["market"].eq("SSE"),
        ["trade_date", "market", "instrument_code", "instrument_name", "shares"],
    ]


def build_plan(
    shares_path: Path,
    *,
    trade_date: str,
    previous_date: str,
    hard_limit: int = 2_000,
) -> dict:
    shares = normalise_sse_shares(pd.read_csv(shares_path, dtype=str))
    current = shares.loc[shares["trade_date"].eq(trade_date)].copy()
    previous = shares.loc[shares["trade_date"].eq(previous_date)].copy()
    if current.empty or previous.empty:
        missing_date = trade_date if current.empty else previous_date
        raise ValueError(f"shares_date_missing:{missing_date}")
    return build_changed_etf_capture_plan(
        current,
        previous,
        target_date=trade_date,
        previous_date=previous_date,
        hard_limit=hard_limit,
    )


def fetch_sse_share_snapshot(
    session: requests.Session,
    *,
    trade_date: str,
) -> tuple[pd.DataFrame, bytes]:
    """Fetch one exact-date SSE ETF share snapshot from the official endpoint."""

    response = session.get(
        SSE_SCALE_URL,
        params={
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": trade_date,
        },
        headers={"Referer": SSE_SCALE_REFERER},
        timeout=(10.0, 45.0),
    )
    if response.status_code in {403, 429}:
        raise RuntimeError(f"sse_share_rate_limited:{response.status_code}")
    if response.status_code != 200:
        raise RuntimeError(f"sse_share_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("sse_share_invalid_json") from exc
    rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"sse_share_date_not_ready:{trade_date}")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("sse_share_row_invalid")
        observed_date = str(row.get("STAT_DATE") or "")[:10]
        if observed_date != trade_date:
            raise ValueError(f"sse_share_date_mismatch:{observed_date}:{trade_date}")
        code = str(row.get("SEC_CODE") or "").strip().zfill(6)
        try:
            shares = Decimal(str(row.get("TOT_VOL") or "")) * Decimal("10000")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"sse_share_invalid_value:{code}") from exc
        if not shares.is_finite() or shares < 0 or shares != shares.to_integral_value():
            raise ValueError(f"sse_share_invalid_value:{code}")
        normalized.append(
            {
                "trade_date": trade_date,
                "market": "SSE",
                "instrument_code": code,
                "instrument_name": str(row.get("SEC_NAME") or code).strip() or code,
                "shares": int(shares),
            }
        )
    return pd.DataFrame(normalized), bytes(response.content)


def build_current_official_plan(
    shares_path: Path,
    *,
    hard_limit: int = 2_000,
    evidence_dir: Path | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Build a plan from the live PCF dates and matching official share facts."""

    client = session or requests.Session()
    known_shares = normalise_sse_shares(pd.read_csv(shares_path, dtype=str))
    known_codes = set(known_shares["instrument_code"].dropna().astype(str).unique().tolist())
    probe_codes = [code for code in ("510300", "510050", "510500") if code in known_codes]
    probe_codes.extend(sorted(known_codes - set(probe_codes)))
    if not probe_codes:
        raise ValueError("sse_share_probe_universe_empty")
    probe_code = probe_codes[0]
    probe_xml = fetch_xml(client, code=probe_code, target_date="current")
    _, header, _ = parse_sse_etf_pcf_xml(
        probe_xml,
        requested_code=probe_code,
        target_date="1900-01-02",
        previous_date="1900-01-01",
    )
    target_date = str(header.get("tradingDay") or "")
    previous_date = str(header.get("preTradingDay") or "")
    if str(header.get("fundInstrumentId") or "") != probe_code:
        raise ValueError("sse_pcf_probe_code_mismatch")
    if len(target_date) != 10 or len(previous_date) != 10 or previous_date >= target_date:
        raise ValueError("sse_pcf_probe_dates_invalid")

    current, current_raw = fetch_sse_share_snapshot(client, trade_date=target_date)
    previous, previous_raw = fetch_sse_share_snapshot(client, trade_date=previous_date)
    plan = build_changed_etf_capture_plan(
        current,
        previous,
        target_date=target_date,
        previous_date=previous_date,
        hard_limit=hard_limit,
    )
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / f"{target_date}-pcf-probe-{probe_code}.xml").write_bytes(probe_xml)
        (evidence_dir / f"{target_date}-sse-etf-scale.json").write_bytes(current_raw)
        (evidence_dir / f"{previous_date}-sse-etf-scale.json").write_bytes(previous_raw)
        evidence = {
            "schemaVersion": 1,
            "probeCode": probe_code,
            "targetDate": target_date,
            "previousDate": previous_date,
            "pcfProbeSha256": hashlib.sha256(probe_xml).hexdigest(),
            "shareSnapshots": {
                target_date: hashlib.sha256(current_raw).hexdigest(),
                previous_date: hashlib.sha256(previous_raw).hexdigest(),
            },
            "dateContract": "PCF TradingDay/PreTradingDay select the exact SSE share snapshots",
        }
        (evidence_dir / f"{target_date}-date-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return plan


def build_live_official_plan(
    *,
    hard_limit: int = 2_000,
    evidence_dir: Path | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Build a plan from the live SSE universe without a market-run artifact.

    PR2 runs PCF independently from the market collector.  A fixed, liquid
    probe ETF supplies the official ``TradingDay``/``PreTradingDay`` pair;
    the two matching SSE scale responses then provide the complete universe.
    The capture and finalize scripts still use the D1 checkpoint control plane
    for idempotent per-ETF recovery.
    """

    client = session or requests.Session()
    probe_code = "510300"
    probe_xml = fetch_xml(client, code=probe_code, target_date="current")
    _, header, _ = parse_sse_etf_pcf_xml(
        probe_xml,
        requested_code=probe_code,
        target_date="1900-01-02",
        previous_date="1900-01-01",
    )
    target_date = str(header.get("tradingDay") or "")
    previous_date = str(header.get("preTradingDay") or "")
    if str(header.get("fundInstrumentId") or "") != probe_code:
        raise ValueError("sse_pcf_probe_code_mismatch")
    if len(target_date) != 10 or len(previous_date) != 10 or previous_date >= target_date:
        raise ValueError("sse_pcf_probe_dates_invalid")

    current, current_raw = fetch_sse_share_snapshot(client, trade_date=target_date)
    previous, previous_raw = fetch_sse_share_snapshot(client, trade_date=previous_date)
    plan = build_changed_etf_capture_plan(
        current,
        previous,
        target_date=target_date,
        previous_date=previous_date,
        hard_limit=hard_limit,
    )
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / f"{target_date}-pcf-probe-{probe_code}.xml").write_bytes(probe_xml)
        (evidence_dir / f"{target_date}-sse-etf-scale.json").write_bytes(current_raw)
        (evidence_dir / f"{previous_date}-sse-etf-scale.json").write_bytes(previous_raw)
        evidence = {
            "schemaVersion": 1,
            "probeCode": probe_code,
            "targetDate": target_date,
            "previousDate": previous_date,
            "pcfProbeSha256": hashlib.sha256(probe_xml).hexdigest(),
            "shareSnapshots": {
                target_date: hashlib.sha256(current_raw).hexdigest(),
                previous_date: hashlib.sha256(previous_raw).hexdigest(),
            },
            "dateContract": "PCF TradingDay/PreTradingDay select the exact SSE share snapshots",
            "universeSource": "live_sse_scale_snapshot",
        }
        (evidence_dir / f"{target_date}-date-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shares", type=Path)
    parser.add_argument("--trade-date")
    parser.add_argument("--previous-date")
    parser.add_argument("--official-current", action="store_true")
    parser.add_argument(
        "--official-live",
        action="store_true",
        help="Fetch the complete live SSE universe; independent of market-run artifacts",
    )
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--hard-limit", type=int, default=2_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.official_current and args.official_live:
        parser.error("--official-current and --official-live are mutually exclusive")
    if args.official_live:
        if args.shares or args.trade_date or args.previous_date:
            parser.error("--official-live cannot be combined with --shares or explicit dates")
        plan = build_live_official_plan(
            hard_limit=args.hard_limit,
            evidence_dir=args.evidence_dir,
        )
    elif args.official_current:
        if not args.shares:
            parser.error("--shares is required with --official-current")
        if args.trade_date or args.previous_date:
            parser.error("--official-current cannot be combined with explicit dates")
        plan = build_current_official_plan(
            args.shares,
            hard_limit=args.hard_limit,
            evidence_dir=args.evidence_dir,
        )
    else:
        if not args.shares or not args.trade_date or not args.previous_date:
            parser.error("--trade-date and --previous-date are required without --official-current")
        plan = build_plan(
            args.shares,
            trade_date=args.trade_date,
            previous_date=args.previous_date,
            hard_limit=args.hard_limit,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "targetDate": plan["targetDate"],
                "status": plan["status"],
                "counts": plan["counts"],
                "planSha256": plan["planSha256"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
