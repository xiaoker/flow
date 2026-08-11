"""Enrich today's ETF-to-stock catalog with the previous release history.

This script is intentionally a small, bounded bridge between the offline
GitHub Actions build and the immutable R2 read models.  It fetches at most the
eight existing stock shards for one previous trading date.  The Worker never
performs this merge.
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
    previous_date: str,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    root = base_url.rstrip("/")
    request_headers = headers or {}
    search = requests.get(
        f"{root}/api/v2/stock-search",
        params={"date": previous_date},
        timeout=timeout,
        headers=request_headers,
    )
    if search.status_code == 404:
        # The previous release predates the stock catalog.  This is the safe
        # first-run path and results in one current node, not fabricated data.
        return []
    search.raise_for_status()
    expected_release_id = search.headers.get("x-flow-release-id")
    if not expected_release_id:
        raise RuntimeError("previous pressure release missing X-Flow-Release-Id")
    search_payload = search.json()
    if not isinstance(search_payload, dict) or search_payload.get("asOf") != previous_date:
        raise RuntimeError("previous pressure search date mismatch")
    pressure = requests.get(
        f"{root}/api/v2/views/pressure",
        params={"date": previous_date},
        timeout=timeout,
        headers=request_headers,
    )
    if pressure.status_code == 503:
        try:
            pressure_error = pressure.json().get("error")
        except ValueError:
            pressure_error = None
        if pressure_error == "view_not_published":
            # The previous release predates this workbench.  Starting the
            # bounded history at the current confirmed node is safe; a
            # present pressure artifact, in contrast, requires all shards.
            return []
    pressure.raise_for_status()
    if pressure.headers.get("x-flow-release-id") != expected_release_id:
        raise RuntimeError("previous pressure artifact release mismatch")
    pressure_payload = pressure.json()
    if not isinstance(pressure_payload, dict) or pressure_payload.get("asOf") != previous_date:
        raise RuntimeError("previous pressure artifact date mismatch")
    previous_items: list[dict[str, Any]] = []
    for shard in SHARDS:
        response = requests.get(
            f"{root}/api/v2/stocks/{shard}",
            params={"date": previous_date},
            timeout=timeout,
            headers=request_headers,
        )
        if response.status_code == 404:
            raise RuntimeError(f"previous pressure release missing shard: {shard}")
        response.raise_for_status()
        if response.headers.get("x-flow-release-id") != expected_release_id:
            raise RuntimeError(f"previous pressure release mismatch: {shard}")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("asOf") != previous_date
            or not isinstance(payload.get("items"), list)
        ):
            raise RuntimeError(f"invalid previous stock shard response: {shard}")
        for item in payload["items"]:
            if isinstance(item, dict):
                enriched = dict(item)
                enriched.setdefault("asOf", previous_date)
                previous_items.append(enriched)
    return previous_items


def main() -> None:
    parser = argparse.ArgumentParser(description="Carry forward ETF pressure history offline")
    parser.add_argument("--catalog", type=Path, required=True, help="Current stock-flow catalog JSON")
    parser.add_argument("--output", type=Path, required=True, help="Enriched catalog JSON")
    parser.add_argument("--previous-date", help="Previous confirmed trading date YYYY-MM-DD")
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
    if args.previous_date:
        if args.previous_shard_dir:
            for shard in SHARDS:
                path = args.previous_shard_dir / f"{shard}.json"
                if path.is_file():
                    previous_items.extend(_read_items(path))
        else:
            access_id = os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", "").strip()
            access_secret = os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", "").strip()
            access_headers = (
                {
                    "CF-Access-Client-Id": access_id,
                    "CF-Access-Client-Secret": access_secret,
                }
                if access_id and access_secret
                else {}
            )
            previous_items = _fetch_previous_items(
                args.base_url,
                args.previous_date,
                args.timeout,
                headers=access_headers,
            )
    enriched = merge_stock_catalog_history(catalog, previous_items)
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
