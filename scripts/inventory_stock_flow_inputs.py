"""Probe archived ETF/PCF inputs without downloading or writing payloads.

The inventory step is deliberately metadata-only. It asks the private history
export whether an immutable normalized object exists for each date, records
its SHA/key/candidate count, and closes the response without reading the body.
This lets a bounded stock-flow backfill choose real dates before any larger
payload is fetched or any derived partition is published.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROBES = (("sse", "etf_scale"), ("szse", "etf_scale"), ("sse", "etf_pcf"))


def _exact_slots(value: str) -> list[dict[str, str]]:
    slots = json.loads(value)
    if not isinstance(slots, list) or not 1 <= len(slots) <= 6:
        raise ValueError("exact slots must contain 1 to 6 entries")
    seen: set[tuple[str, str]] = set()
    for slot in slots:
        if not isinstance(slot, dict) or set(slot) != {"date", "source"}:
            raise ValueError("exact slot requires only date and source")
        day, source = slot["date"], slot["source"]
        if not isinstance(day, str) or not ISO_DATE.fullmatch(day):
            raise ValueError("exact slot requires ISO date")
        date.fromisoformat(day)
        if source not in ("sse", "szse"):
            raise ValueError("exact slot source must be sse or szse")
        identity = (day, source)
        if identity in seen:
            raise ValueError("duplicate exact slot")
        seen.add(identity)
    return sorted(slots, key=lambda slot: (slot["date"], slot["source"]))


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
    last_code = None
    for attempt in range(attempts):
        try:
            response = session.get(endpoint, params=params, headers=headers, timeout=(10, 45), stream=True, allow_redirects=False)
            try:
                last_code = response.status_code
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
    return {"status": "error", "code": last_code, "detail": last_error}


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory archived ETF/PCF inputs without downloading bodies")
    parser.add_argument("--base-url", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--dates", type=Path)
    scope.add_argument("--slots-json", help="1–6 exact date/source ETF-share slots; no PCF or date discovery")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=2)
    args = parser.parse_args()
    if args.attempts <= 0:
        raise ValueError("attempts must be positive")

    slots = _exact_slots(args.slots_json) if args.slots_json is not None else None
    if slots is not None and args.attempts != 1:
        raise ValueError("exact slots require exactly one attempt")
    if slots is not None:
        if args.base_url.rstrip("/") != "https://flow.xiaoker.com":
            raise ValueError("exact inventory requires the official Flow origin")
        if any(not os.environ.get(name, "").strip() for name in (
            "FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"
        )):
            raise ValueError("exact inventory requires existing backend authentication")

    endpoint = args.base_url.rstrip("/") + "/api/v1/admin/history-artifact"
    headers = {
        "Authorization": f"Bearer {os.environ.get('FLOW_INGEST_API_KEY', '')}",
        "CF-Access-Client-Id": os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", ""),
    }
    session = requests.Session()
    if slots is not None:
        report = {"schemaVersion": 1, "mode": "metadata_only", "scope": "exact_etf_share_slots",
                  "requestedSlots": slots, "complete": False, "slots": []}
        for slot in slots:
            result = _probe(session, endpoint=endpoint, date_value=slot["date"],
                            source=slot["source"], dataset="etf_scale", headers=headers, attempts=1)
            report["slots"].append({**slot, **result})
            if result["status"] not in ("ready", "missing"):
                break  # Unknown/auth is evidence, not permission to expand scope or retry.
        report["complete"] = len(report["slots"]) == len(slots) and all(
            result["status"] in ("ready", "missing") for result in report["slots"])
        session.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"mode": report["mode"], "requestedSlots": len(slots), "probedSlots": len(report["slots"])}))
        return
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
