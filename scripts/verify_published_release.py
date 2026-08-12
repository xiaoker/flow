"""Bounded post-publish canary for one immutable read-model release."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests


VIEWS = ("overview", "radar", "radar_flow", "rotation", "national", "connect", "margin", "pressure")


def _headers() -> dict[str, str]:
    access_id = os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", "").strip()
    access_secret = os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", "").strip()
    if not access_id or not access_secret:
        raise RuntimeError("missing Cloudflare Access headers")
    return {"CF-Access-Client-Id": access_id, "CF-Access-Client-Secret": access_secret}


def verify(base_url: str, *, expected_release_id: str, expected_as_of: str, timeout: float = 20.0) -> dict[str, Any]:
    root = base_url.rstrip("/")
    headers = _headers()

    def get(path: str, params: dict[str, str] | None = None) -> requests.Response:
        response = requests.get(
            f"{root}{path}",
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
        release_id = response.headers.get("x-flow-release-id")
        if release_id and release_id != expected_release_id:
            raise RuntimeError(f"{path} selected {release_id}, expected {expected_release_id}")
        return response

    latest_response = get("/api/v2/releases/latest")
    latest = latest_response.json()
    if not isinstance(latest, dict) or latest.get("releaseId") != expected_release_id:
        raise RuntimeError(f"latest release mismatch: {latest}")
    if latest.get("asOf") != expected_as_of or latest.get("observedAt") is None:
        raise RuntimeError(f"latest asOf mismatch: {latest}")
    for path in ("/api/v2/dashboard", "/api/v2/history/dates"):
        get(path)
    # A stale CDN/proxy response must not be mistaken for a successful canary.
    # The release endpoint is private/no-store, but also exercise its ETag
    # path whenever the deployment supplies one.
    latest_etag = latest_response.headers.get("etag")
    if latest_etag:
        cached = requests.get(
            f"{root}/api/v2/releases/latest",
            headers={**headers, "If-None-Match": latest_etag},
            timeout=timeout,
            allow_redirects=False,
        )
        if cached.status_code not in {200, 304}:
            cached.raise_for_status()
    get("/api/v2/dashboard", {"date": expected_as_of})
    for view in VIEWS:
        response = get(f"/api/v2/views/{view}", {"date": expected_as_of})
        payload = response.json()
        if isinstance(payload, dict) and payload.get("asOf") not in {None, expected_as_of}:
            raise RuntimeError(f"{view} asOf mismatch")
    get("/api/v2/stock-search", {"date": expected_as_of})
    shard = get("/api/v2/stocks/stock_0", {"date": expected_as_of})
    etag = shard.headers.get("etag")
    if etag:
        not_modified = requests.get(
            f"{root}/api/v2/stocks/stock_0",
            params={"date": expected_as_of},
            headers={**headers, "If-None-Match": etag},
            timeout=timeout,
            allow_redirects=False,
        )
        if not_modified.status_code not in {200, 304}:
            not_modified.raise_for_status()
    return {"releaseId": expected_release_id, "asOf": expected_as_of, "checkedViews": len(VIEWS)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an immutable Flow read-model release")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    print(json.dumps(verify(args.base_url, expected_release_id=args.release_id, expected_as_of=args.as_of, timeout=args.timeout)))


if __name__ == "__main__":
    main()
