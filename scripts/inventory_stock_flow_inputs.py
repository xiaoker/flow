"""Probe archived ETF/PCF inputs without downloading or writing payloads.

The inventory step is deliberately metadata-only. It asks the private history
export whether an immutable normalized object exists for each date, records
its SHA/key/candidate count, and closes the response without reading the body.
This lets a bounded stock-flow backfill choose real dates before any larger
payload is fetched or any derived partition is published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROBES = (("sse", "etf_scale"), ("szse", "etf_scale"), ("sse", "etf_pcf"))


def _dates(path: Path) -> list[str]:
    values = sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()})
    if not values or any(not ISO_DATE.fullmatch(value) for value in values):
        raise ValueError("dates must contain at least one ISO date")
    return values


def _probe(
    session: requests.Session,
    *,
    endpoint: str,
    date_value: str,
    source: str,
    dataset: str,
    headers: dict[str, str],
    attempts: int,
) -> dict[str, Any]:
    params = {"date": date_value, "source": source, "dataset": dataset, "file": "data"}
    last_error = "not_requested"
    for attempt in range(attempts):
        try:
            response = session.get(endpoint, params=params, headers=headers, timeout=(10, 45), stream=True)
            try:
                result: dict[str, Any] = {
                    "status": "ready" if response.ok else "missing" if response.status_code == 404 else "error",
                    "code": response.status_code,
                    "sha256": response.headers.get("x-flow-normalized-sha256"),
                    "key": response.headers.get("x-flow-normalized-key"),
                    "candidateCount": int(response.headers.get("x-flow-normalized-candidate-count", "0") or 0),
                    "contentLength": response.headers.get("content-length"),
                }
                if response.status_code == 404:
                    return result
                if not response.ok:
                    last_error = f"HTTP {response.status_code}"
                    continue
                if not result["sha256"] or not result["key"]:
                    return {**result, "status": "invalid_metadata", "detail": "missing lineage headers"}
                return result
            finally:
                response.close()
        except Exception as exc:  # transport only; no source response is fabricated
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < attempts:
            time.sleep(2**attempt)
    return {"status": "error", "code": None, "detail": last_error}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory archived ETF/PCF inputs without downloading bodies")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=2)
    args = parser.parse_args()
    if args.attempts <= 0:
        raise ValueError("attempts must be positive")

    endpoint = args.base_url.rstrip("/") + "/api/v1/admin/history-artifact"
    headers = {
        "Authorization": f"Bearer {os.environ.get('FLOW_INGEST_API_KEY', '')}",
        "CF-Access-Client-Id": os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", ""),
    }
    session = requests.Session()
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": "metadata_only",
        "endpoint": endpoint,
        "dates": {},
        "availableDates": [],
        "completeShareDates": [],
        "pcfReadyDates": [],
    }
    for date_value in _dates(args.dates):
        statuses: dict[str, Any] = {}
        for source, dataset in PROBES:
            statuses[f"{source}_{dataset}"] = _probe(
                session,
                endpoint=endpoint,
                date_value=date_value,
                source=source,
                dataset=dataset,
                headers=headers,
                attempts=args.attempts,
            )
        report["dates"][date_value] = statuses
        sse_ready = statuses["sse_etf_scale"].get("status") == "ready"
        szse_ready = statuses["szse_etf_scale"].get("status") == "ready"
        pcf_ready = statuses["sse_etf_pcf"].get("status") == "ready"
        if pcf_ready:
            report["pcfReadyDates"].append(date_value)
        if sse_ready and pcf_ready:
            report["availableDates"].append(date_value)
        if sse_ready and szse_ready and pcf_ready:
            report["completeShareDates"].append(date_value)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "requestedDates": len(report["dates"]),
                "pcfReadyDates": len(report["pcfReadyDates"]),
                "availableDates": len(report["availableDates"]),
                "completeShareDates": len(report["completeShareDates"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
