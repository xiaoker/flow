"""Run a bounded, read-only shadow comparison for ETF adjacent changes.

The command is intentionally offline: it compares a contract generated from a
canonical CSV with a previously captured legacy result.  ``--candidate-date``
may be supplied at most twice so a one-time production shadow cannot turn back
into a daily full-history D1 query.  Nothing is activated or written remotely.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from trading_desk.etf_daily_changes import (
    build_official_etf_daily_changes,
    reconcile_official_etf_daily_changes,
)


def _read_json(path: Path) -> Any:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value


def _legacy_rows(payload: Any, *, as_of: str) -> list[dict[str, Any]]:
    """Accept either a raw legacy row list or an ``{asOf, rows}`` envelope."""

    if isinstance(payload, dict):
        if payload.get("asOf") is not None and str(payload["asOf"]) != as_of:
            raise ValueError(f"legacy result date mismatch: {payload.get('asOf')} != {as_of}")
        payload = payload.get("rows") or []
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("legacy input must be a JSON row list or an {asOf,rows} object")
    return [dict(row) for row in payload]


def _candidate_dates(args: argparse.Namespace, frame: pd.DataFrame) -> list[str]:
    explicit = [str(value) for value in args.candidate_date]
    if len(explicit) > 2:
        raise ValueError("at most two production candidate dates are allowed")
    if len(set(explicit)) != len(explicit):
        raise ValueError("candidate dates must be unique")
    if explicit:
        return explicit
    if args.as_of:
        return [str(args.as_of)]
    if "trade_date" not in frame.columns or frame["trade_date"].dropna().empty:
        raise ValueError("canonical input has no candidate date")
    return [str(frame["trade_date"].dropna().astype(str).max())]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded read-only ETF adjacent-change shadow reconciliation")
    parser.add_argument("--canonical", type=Path, required=True, help="canonical ETF CSV containing one or more dates")
    parser.add_argument("--legacy", type=Path, required=True, help="captured legacy query JSON (rows or {asOf,rows})")
    parser.add_argument("--as-of", help="single candidate date when --candidate-date is not used")
    parser.add_argument(
        "--candidate-date",
        action="append",
        default=[],
        help="candidate date; repeat at most twice for the one-time shadow",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frame = pd.read_csv(args.canonical, dtype={"instrument_code": str})
    dates = _candidate_dates(args, frame)
    if args.as_of and args.candidate_date:
        raise ValueError("use either --as-of or --candidate-date, not both")
    legacy_payload = _read_json(args.legacy)
    reports: list[dict[str, Any]] = []
    for as_of in dates:
        contract = build_official_etf_daily_changes(frame, as_of=as_of)
        legacy = _legacy_rows(legacy_payload, as_of=as_of)
        comparison = reconcile_official_etf_daily_changes(contract, legacy)
        reports.append(
            {
                "asOf": as_of,
                "expectedPreviousDate": contract["expectedPreviousDate"],
                "contract": contract,
                "comparison": comparison,
            }
        )
    report = {
        "schemaVersion": 1,
        "shadowOnly": True,
        "activated": False,
        "candidateLimit": 2,
        "candidateCount": len(reports),
        "status": "pass" if all(item["comparison"]["status"] == "pass" for item in reports) else "mismatch",
        "candidates": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": report["status"],
                "shadowOnly": report["shadowOnly"],
                "activated": report["activated"],
                "candidateCount": report["candidateCount"],
            },
            ensure_ascii=False,
        )
    )
    if report["status"] != "pass":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
