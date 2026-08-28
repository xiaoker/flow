#!/usr/bin/env python3
"""Audit whether legacy PCF rows may enter the exact-date PCF v2 history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = {"trade_date", "pcf_date", "pcf_previous_date", "date_basis"}


def audit_legacy_history(frame: pd.DataFrame, dates: list[str]) -> dict:
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"legacy_pcf_missing_columns:{','.join(missing)}")
    value = frame.copy()
    for column in ("trade_date", "pcf_date", "pcf_previous_date"):
        value[column] = value[column].astype(str).str[:10]
    value["date_basis"] = value["date_basis"].astype(str)
    results: list[dict] = []
    for trade_date in dates:
        rows = value.loc[value["trade_date"].eq(trade_date)].copy()
        if rows.empty:
            results.append(
                {
                    "tradeDate": trade_date,
                    "status": "MISSING_LEGACY_EVIDENCE",
                    "eligibleForV2": False,
                    "rows": 0,
                    "recommendedAction": "record_gap_and_collect_future_exact_node",
                }
            )
            continue
        pcf_dates = sorted(rows["pcf_date"].dropna().unique().tolist())
        previous_dates = sorted(rows["pcf_previous_date"].dropna().unique().tolist())
        date_bases = sorted(rows["date_basis"].dropna().unique().tolist())
        exact = pcf_dates == [trade_date] and all("next_day" not in basis for basis in date_bases)
        status = "RAW_QA_EVIDENCE_REQUIRED" if exact else "LEGACY_ESTIMATE_ONLY"
        results.append(
            {
                "tradeDate": trade_date,
                "status": status,
                "eligibleForV2": False,
                "rows": len(rows),
                "pcfDates": pcf_dates,
                "pcfPreviousDates": previous_dates,
                "dateBasis": date_bases,
                "recommendedAction": (
                    "verify_raw_normalized_qa_hashes_before_migration"
                    if exact
                    else "keep_as_legacy_only_and_collect_future_exact_node"
                ),
            }
        )
    return {
        "schemaVersion": 1,
        "contract": "pcf_date == trade_date AND raw/normalized/QA hashes agree",
        "requestedDates": dates,
        "eligibleDates": [item["tradeDate"] for item in results if item["eligibleForV2"]],
        "ineligibleDates": [item["tradeDate"] for item in results if not item["eligibleForV2"]],
        "dates": results,
        "policy": {
            "copyCurrentNode": False,
            "relabelNextDayBasket": False,
            "missingHistoryIsExplicit": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcf", type=Path, required=True)
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_legacy_history(pd.read_csv(args.pcf, dtype=str), sorted(dict.fromkeys(args.dates)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "eligibleDates": len(report["eligibleDates"]),
                "ineligibleDates": len(report["ineligibleDates"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
