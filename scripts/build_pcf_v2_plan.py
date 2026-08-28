#!/usr/bin/env python3
"""Build one deterministic SSE PCF v2 capture plan from ETF share snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from trading_desk.pcf_capture import build_changed_etf_capture_plan


ALIASES = {
    "SEC_CODE": "instrument_code",
    "基金代码": "instrument_code",
    "SEC_NAME": "instrument_name",
    "基金简称": "instrument_name",
    "TOT_VOL": "shares",
    "当前规模(份)": "shares",
}


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shares", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-date", required=True)
    parser.add_argument("--hard-limit", type=int, default=2_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
