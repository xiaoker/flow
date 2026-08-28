#!/usr/bin/env python3
"""Summarize authoritative D1 PCF v2 jobs for a consecutive trading-day gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import requests

from scripts.capture_sse_pcf_v2 import _auth_headers


def summarize_jobs(observations: list[dict[str, Any]], dates: list[str], *, required_days: int) -> dict[str, Any]:
    by_date = {str(item.get("tradeDate") or ""): item for item in observations}
    rows: list[dict[str, Any]] = []
    passing_dates: list[str] = []
    for index, trade_date in enumerate(dates):
        item = by_date.get(trade_date, {"tradeDate": trade_date, "status": "MISSING"})
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        required = int(job.get("required_count", 0) or 0)
        captured = int(job.get("captured_count", 0) or 0)
        valid = int(job.get("valid_count", 0) or 0)
        failed = int(job.get("failed_count", 0) or 0)
        expected_previous = dates[index - 1] if index > 0 else None
        exact_previous = expected_previous is None or str(job.get("previous_date") or "") == expected_previous
        ready = (
            item.get("status") in {"READY", "READY_NO_CHANGE"}
            and required == captured == valid
            and failed == 0
            and int(job.get("coverage_ppm", 0) or 0) == 1_000_000
            and exact_previous
        )
        if ready:
            passing_dates.append(trade_date)
        rows.append(
            {
                "tradeDate": trade_date,
                "status": item.get("status", "MISSING"),
                "ready": ready,
                "previousDate": job.get("previous_date"),
                "expectedPreviousDate": expected_previous,
                "coverage": {"required": required, "captured": captured, "valid": valid, "failed": failed},
                "manifestKey": job.get("canonical_manifest_key"),
            }
        )
    observed = sum(row["status"] != "MISSING" for row in rows)
    return {
        "schemaVersion": 1,
        "status": "PASS" if len(dates) >= required_days and len(passing_dates) == len(dates) else "INSUFFICIENT_OBSERVATIONS" if observed < required_days else "FAIL",
        "requiredDays": required_days,
        "observedDays": observed,
        "passingDays": len(passing_dates),
        "dates": dates,
        "passingDates": passing_dates,
        "failedDates": [row["tradeDate"] for row in rows if not row["ready"]],
        "observations": rows,
        "guardrails": {
            "exactDateContract": all(row["ready"] for row in rows) if rows else False,
            "productionReadFlagChanged": False,
            "productionPublishCalls": 0,
        },
    }


def fetch_observations(
    *,
    base_url: str,
    dates: list[str],
    api_key: str,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> list[dict[str, Any]]:
    session = requests.Session()
    headers = _auth_headers(
        api_key,
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    observations: list[dict[str, Any]] = []
    for trade_date in dates:
        response = session.get(
            f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture",
            params={"date": trade_date},
            headers=headers,
            timeout=(10.0, 45.0),
        )
        if response.status_code == 404:
            observations.append({"tradeDate": trade_date, "status": "MISSING"})
            continue
        response.raise_for_status()
        payload = response.json()
        job = payload.get("job") if isinstance(payload, dict) else None
        observations.append(
            {
                "tradeDate": trade_date,
                "status": str((job or {}).get("status") or "UNKNOWN"),
                "job": job or {},
            }
        )
    return observations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://flow.xiaoker.com")
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--required-days", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dates = sorted(dict.fromkeys(line.strip() for line in args.dates.read_text(encoding="utf-8").splitlines() if line.strip()))
    observations = fetch_observations(
        base_url=args.base_url,
        dates=dates,
        api_key=os.environ.get("FLOW_INGEST_API_KEY", ""),
        access_client_id=os.environ.get("FLOW_CF_ACCESS_CLIENT_ID") or None,
        access_client_secret=os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET") or None,
    )
    summary = summarize_jobs(observations, dates, required_days=args.required_days)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": summary["status"], "observedDays": summary["observedDays"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
