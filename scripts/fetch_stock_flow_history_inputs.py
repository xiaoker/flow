"""Fetch bounded, archived ETF/PCF inputs for the PR3 offline builder.

The endpoint reads one immutable normalized R2 object at a time.  This script
keeps the Worker request small and records missing dates instead of replacing
them with a latest-value fallback.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests


class LineageIntegrityError(RuntimeError):
    """Raised when archived bytes disagree with their declared lineage."""


def _fetch(
    session: requests.Session,
    *,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    attempts: int,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    last_detail = "not_requested"
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, headers=headers, timeout=(10, 90))
            if response.status_code == 404:
                return None, {"status": "missing", "code": 404}
            if response.status_code == 409:
                raise RuntimeError(f"ambiguous archived artifact: {params}")
            response.raise_for_status()
            wire_content = response.content
            content = wire_content
            if response.headers.get("content-type", "").startswith("application/gzip"):
                content = gzip.decompress(content)
            wire_sha256 = hashlib.sha256(wire_content).hexdigest()
            content_sha256 = hashlib.sha256(content).hexdigest()
            declared_sha256 = response.headers.get("x-flow-normalized-sha256", "").strip().lower()
            if declared_sha256 and declared_sha256 not in {wire_sha256, content_sha256}:
                raise LineageIntegrityError(
                    "archived artifact sha256 mismatch: "
                    f"declared={declared_sha256}:wire={wire_sha256}:content={content_sha256}"
                )
            # Legacy normalized objects predate custom SHA metadata.  The R2
            # object key is immutable, so hashing the exact response body here
            # restores a verifiable lineage without rewriting or fabricating
            # the archived source.  Newer history-import objects expose the
            # normalized-content SHA, while PCF v2 pointers expose the stored
            # gzip-object SHA; accept either only after byte-level verification.
            lineage_sha256 = declared_sha256 or wire_sha256
            sha256_source = (
                "header-verified-object"
                if declared_sha256 == wire_sha256
                else "header-verified-content"
                if declared_sha256 == content_sha256
                else "computed-object-body"
            )
            frame = pd.read_csv(io.BytesIO(content), dtype=str)
            if frame.empty:
                return None, {"status": "empty", "code": response.status_code}
            return frame, {
                "status": "ready",
                "code": response.status_code,
                "sha256": lineage_sha256,
                "sha256Source": sha256_source,
                "wireSha256": wire_sha256,
                "contentSha256": content_sha256,
                "key": response.headers.get("x-flow-normalized-key"),
                "candidateCount": int(response.headers.get("x-flow-normalized-candidate-count", "1")),
                "selection": response.headers.get("x-flow-normalized-selection", "single"),
                "rows": len(frame),
            }
        except LineageIntegrityError as exc:
            last_detail = str(exc)
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            raise
        except Exception as exc:
            last_detail = str(exc)
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"archived artifact fetch failed for {params}: {last_detail}")


def _with_market(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    value = frame.copy()
    if "market" not in value.columns:
        value["market"] = market
    else:
        value["market"] = value["market"].fillna(market).astype(str).str.upper()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch PR3 ETF/PCF history inputs from archived R2 objects")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--dates", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("stock-flow-fetch-report.json"))
    parser.add_argument("--inventory", type=Path, default=Path("pcf-inventory.json"))
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--require-current", action="store_true")
    args = parser.parse_args()
    if args.attempts <= 0:
        raise ValueError("attempts must be positive")

    dates = sorted({line.strip() for line in args.dates.read_text(encoding="utf-8").splitlines() if line.strip()})
    if not dates or args.as_of not in dates:
        raise ValueError("dates must include as-of")
    headers = {
        "Authorization": f"Bearer {os.environ.get('FLOW_INGEST_API_KEY', '')}",
        "CF-Access-Client-Id": os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", ""),
    }
    endpoint = args.base_url.rstrip("/") + "/api/v1/admin/history-artifact"
    session = requests.Session()
    shares: list[pd.DataFrame] = []
    pcfs: list[pd.DataFrame] = []
    report: dict[str, Any] = {"asOf": args.as_of, "dates": {}, "availableDates": []}
    pcf_inventory: list[dict[str, Any]] = []
    for trade_date in dates:
        date_report: dict[str, Any] = {}
        for source in ("sse", "szse"):
            frame, status = _fetch(
                session,
                url=endpoint,
                params={"date": trade_date, "source": source, "dataset": "etf_scale", "file": "data"},
                headers=headers,
                attempts=args.attempts,
            )
            date_report[f"{source}_etf_scale"] = status
            if frame is not None:
                shares.append(_with_market(frame, source.upper()))
        pcf, pcf_status = _fetch(
            session,
            url=endpoint,
            params={"date": trade_date, "source": "sse", "dataset": "etf_pcf", "file": "data"},
            headers=headers,
            attempts=args.attempts,
        )
        date_report["sse_etf_pcf"] = pcf_status
        pcf_inventory.append(
            {
                "tradeDate": trade_date,
                "source": "sse",
                "dataset": "etf_pcf",
                **pcf_status,
            }
        )
        if pcf is not None:
            pcfs.append(_with_market(pcf, "SSE"))
        if pcf is not None and any(date_report.get(f"{source}_etf_scale", {}).get("status") == "ready" for source in ("sse", "szse")):
            report["availableDates"].append(trade_date)
        report["dates"][trade_date] = date_report

    args.output_dir.mkdir(parents=True, exist_ok=True)
    share_frame = pd.concat(shares, ignore_index=True) if shares else pd.DataFrame()
    pcf_frame = pd.concat(pcfs, ignore_index=True) if pcfs else pd.DataFrame()
    share_frame.to_csv(args.output_dir / "shares.csv", index=False)
    pcf_frame.to_csv(args.output_dir / "pcf.csv", index=False)
    report["shareRows"] = len(share_frame)
    report["pcfRows"] = len(pcf_frame)
    report["availableDates"] = sorted(set(report["availableDates"]))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.inventory.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asOf": args.as_of,
                "calculationVersion": "etf-stock-pressure-v2",
                "entries": pcf_inventory,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if args.require_current and args.as_of not in report["availableDates"]:
        raise RuntimeError("current ETF/PCF archived inputs are incomplete")
    print(json.dumps({"outputDir": str(args.output_dir), "availableDates": len(report["availableDates"]), "shareRows": len(share_frame), "pcfRows": len(pcf_frame)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except LineageIntegrityError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_QUALITY",
                    "errorCode": "stock_flow_history_lineage_mismatch",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(42) from exc
