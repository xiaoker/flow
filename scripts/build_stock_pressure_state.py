"""Enrich today's ETF-to-stock catalog with the previous release history.

This script is intentionally a small, bounded bridge between the offline
GitHub Actions build and the immutable R2 read models. It fetches at most one
candidate's search object, pressure object, and eight stock shards. The Worker
never performs this merge.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import requests

from trading_desk.stock_pressure import merge_stock_catalog_history


SHARDS = tuple(f"stock_{index}" for index in range(8))


def _read_items(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError(f"invalid stock shard: {path}")
    as_of = str(payload.get("asOf", ""))
    return [dict(item, asOf=as_of) for item in payload["items"] if isinstance(item, dict)]


def _fetch_previous_items(
    base_url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    *,
    before_date: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = base_url.rstrip("/")
    request_headers = headers or {}
    candidates = requests.get(
        f"{root}/api/v1/admin/releases/previous-candidates",
        params={"before": before_date or "9999-12-31", "limit": 10},
        timeout=timeout,
        headers=request_headers,
    )
    if candidates.status_code == 404:
        # The previous release predates the stock catalog.  This is the safe
        # first-run path and results in one current node, not fabricated data.
        return [], {"historyStatus": "bootstrapped", "previousReleaseId": None}
    candidates.raise_for_status()
    candidate_payload = candidates.json()
    rows = candidate_payload.get("candidates") if isinstance(candidate_payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("previous release candidates response is invalid")
    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        release_id = str(candidate.get("releaseId") or "").strip()
        candidate_date = str(candidate.get("asOf") or "").strip()
        if not release_id or not candidate_date:
            raise RuntimeError("previous release candidate identity is invalid")

        def fetch_artifact(name: str) -> dict[str, Any]:
            response = requests.get(
                f"{root}/api/v1/admin/releases/artifact",
                params={"releaseId": release_id, "name": name},
                timeout=timeout,
                headers=request_headers,
            )
            if response.status_code == 404:
                raise RuntimeError(f"previous pressure release missing artifact: {name}")
            response.raise_for_status()
            if response.headers.get("x-flow-release-id") != release_id:
                raise RuntimeError(f"previous pressure artifact release mismatch: {name}")
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("asOf") != candidate_date:
                raise RuntimeError(f"previous pressure artifact date mismatch: {name}")
            return payload

        pressure = fetch_artifact("pressure")
        pressure_data = pressure.get("data", {}).get("pressure") if isinstance(pressure.get("data"), dict) else None
        pressure_schema = pressure_data.get("pressureSchemaVersion", pressure_data.get("schemaVersion", 0)) if isinstance(pressure_data, dict) else 0
        if int(pressure_schema) == 1:
            # A valid legacy pressure v1 release is incompatible, but it is
            # not evidence that a later v2 release is damaged.
            continue
        if not isinstance(pressure_schema, (int, float)) or int(pressure_schema) != 2:
            raise RuntimeError("previous pressure artifact schema is unsupported")
        search = fetch_artifact("stock_search")
        if not isinstance(search.get("items"), list):
            raise RuntimeError("previous stock search artifact is invalid")
        previous_items: list[dict[str, Any]] = []
        for shard in SHARDS:
            payload = fetch_artifact(shard)
            for item in payload.get("items", []):
                if isinstance(item, dict):
                    enriched = dict(item)
                    enriched.setdefault("asOf", candidate_date)
                    previous_items.append(enriched)
        if not previous_items:
            raise RuntimeError("previous compatible release has no stock items")
        return previous_items, {
            "historyStatus": "continued",
            "previousReleaseId": release_id,
            "previousAsOf": candidate_date,
        }
    return [], {"historyStatus": "bootstrapped", "previousReleaseId": None}


def main() -> None:
    parser = argparse.ArgumentParser(description="Carry forward ETF pressure history offline")
    parser.add_argument("--catalog", type=Path, required=True, help="Current stock-flow catalog JSON")
    parser.add_argument("--output", type=Path, required=True, help="Enriched catalog JSON")
    parser.add_argument("--before-date", help="Current release date used to bound candidates")
    parser.add_argument(
        "--previous-shard-dir",
        type=Path,
        help="Optional local directory containing stock_0.json through stock_7.json",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"),
        help="Public Flow URL used only by the offline build",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    previous_items: list[dict[str, Any]] = []
    continuity: dict[str, Any] = {"historyStatus": "bootstrapped", "previousReleaseId": None}
    if args.before_date:
        if args.previous_shard_dir:
            for shard in SHARDS:
                path = args.previous_shard_dir / f"{shard}.json"
                if not path.is_file():
                    raise RuntimeError(f"previous pressure release missing local shard: {shard}")
                previous_items.extend(_read_items(path))
            continuity = {
                "historyStatus": "continued",
                "previousReleaseId": None,
                "previousAsOf": args.before_date,
            }
            if not previous_items:
                raise RuntimeError("previous compatible local release has no stock items")
        else:
            access_id = os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", "").strip()
            access_secret = os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", "").strip()
            access_headers = (
                {
                    "Authorization": f"Bearer {os.environ.get('FLOW_INGEST_API_KEY', '').strip()}",
                    "CF-Access-Client-Id": access_id,
                    "CF-Access-Client-Secret": access_secret,
                }
                if access_id and access_secret and os.environ.get("FLOW_INGEST_API_KEY", "").strip()
                else {}
            )
            previous_items, continuity = _fetch_previous_items(
                args.base_url,
                args.timeout,
                headers=access_headers,
                before_date=args.before_date,
            )
    enriched = merge_stock_catalog_history(catalog, previous_items)
    if continuity.get("historyStatus") == "continued":
        previous_ids = {
            f"{item.get('listingMarket')}:{item.get('stockCode')}"
            for item in previous_items
            if item.get("listingMarket") and item.get("stockCode")
        }
        current_ids = {
            f"{item.get('listingMarket')}:{item.get('stockCode')}"
            for item in catalog.get("items", [])
            if item.get("listingMarket") and item.get("stockCode")
        }
        matched = len(previous_ids & current_ids)
        previous_count = len(previous_ids)
        match_rate = round(matched / previous_count * 100, 2) if previous_count else 0.0
        if previous_count and match_rate < 80:
            raise RuntimeError(f"previous stock match rate below 80%: {match_rate}")
        if match_rate < 95:
            continuity["historyWarnings"] = [
                f"previous stock match rate is below 95%: {match_rate}%"
            ]
        continuity.update({
            "previousStockCount": previous_count,
            "currentStockCount": len(current_ids),
            "matchedStockCount": matched,
            "matchRate": match_rate,
        })
        continuity["historyContinuity"] = {
            "previousReleaseId": continuity.get("previousReleaseId"),
            "previousAsOf": continuity.get("previousAsOf"),
            "previousStockCount": previous_count,
            "currentStockCount": len(current_ids),
            "matchedStockCount": matched,
            "matchRate": match_rate,
        }
    enriched.update(continuity)
    if continuity.get("historyWarnings"):
        enriched["warnings"] = [
            *list(enriched.get("warnings") or []),
            *continuity["historyWarnings"],
        ]
    args.output.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "asOf": enriched.get("asOf"),
                "stocks": len(enriched.get("items") or []),
                "previousItems": len(previous_items),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
