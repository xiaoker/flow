"""Bounded post-publish canary for one immutable read-model release."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests


MARKET_VIEWS = ("overview", "radar", "rotation", "national", "connect", "margin")
PENETRATION_VIEWS = ("radar_flow", "pressure")
FULL_VIEWS = (*MARKET_VIEWS, *PENETRATION_VIEWS)
MARKET_ARTIFACTS = (
    "bootstrap",
    "overview",
    "radar",
    "rotation",
    "national",
    "connect",
    "margin",
)
PENETRATION_ARTIFACTS = (
    "radar_flow",
    "pressure",
    "stock_search",
    "stock_0",
    "stock_1",
    "stock_2",
    "stock_3",
    "stock_4",
    "stock_5",
    "stock_6",
    "stock_7",
)
FULL_ARTIFACTS = (*MARKET_ARTIFACTS, *PENETRATION_ARTIFACTS)
# Backward-compatible name used by the legacy full-release canary tests and
# callers. New workflows should select an explicit slot instead.
CANDIDATE_ARTIFACTS = FULL_ARTIFACTS


def slot_contract(slot: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if slot == "dashboard":
        return MARKET_VIEWS, MARKET_ARTIFACTS
    if slot == "etf_penetration":
        return PENETRATION_VIEWS, PENETRATION_ARTIFACTS
    if slot == "full":
        return FULL_VIEWS, FULL_ARTIFACTS
    raise ValueError(f"unsupported release slot: {slot}")


def _headers() -> dict[str, str]:
    access_id = os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", "").strip()
    access_secret = os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", "").strip()
    if not access_id or not access_secret:
        raise RuntimeError("missing Cloudflare Access headers")
    return {"CF-Access-Client-Id": access_id, "CF-Access-Client-Secret": access_secret}


def _candidate_headers() -> dict[str, str]:
    ingest_key = os.environ.get("FLOW_INGEST_API_KEY", "").strip()
    if not ingest_key:
        raise RuntimeError("missing FLOW_INGEST_API_KEY for candidate canary")
    return {**_headers(), "Authorization": f"Bearer {ingest_key}"}


def verify(
    base_url: str,
    *,
    expected_release_id: str,
    expected_as_of: str,
    expected_build_sha: str | None = None,
    repeat: int = 1,
    timeout: float = 20.0,
    slot: str = "full",
) -> dict[str, Any]:
    if not 1 <= repeat <= 100:
        raise ValueError("repeat must be between 1 and 100")

    views, _artifacts = slot_contract(slot)
    root = base_url.rstrip("/")
    headers = _headers()
    request_count = 0

    def get(path: str, params: dict[str, str] | None = None) -> requests.Response:
        nonlocal request_count
        request_count += 1
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

    latest: dict[str, Any] = {}
    for iteration in range(1, repeat + 1):
        latest_response = get("/api/v2/releases/latest")
        payload = latest_response.json()
        if not isinstance(payload, dict) or payload.get("releaseId") != expected_release_id:
            raise RuntimeError(f"latest release mismatch on iteration {iteration}: {payload}")
        if payload.get("asOf") != expected_as_of or payload.get("observedAt") is None:
            raise RuntimeError(f"latest asOf mismatch on iteration {iteration}: {payload}")
        if expected_build_sha and payload.get("buildSha") != expected_build_sha:
            raise RuntimeError(
                f"latest build SHA mismatch on iteration {iteration}: "
                f"{payload.get('buildSha')} != {expected_build_sha}"
            )
        latest = payload
        for path in ("/api/v2/dashboard", "/api/v2/history/dates"):
            get(path)
        # A stale CDN/proxy response must not be mistaken for a successful canary.
        # Exercise its ETag path once per iteration when the deployment supplies one.
        latest_etag = latest_response.headers.get("etag")
        if latest_etag:
            request_count += 1
            cached = requests.get(
                f"{root}/api/v2/releases/latest",
                headers={**headers, "If-None-Match": latest_etag},
                timeout=timeout,
                allow_redirects=False,
            )
            if cached.status_code not in {200, 304}:
                cached.raise_for_status()
        get("/api/v2/dashboard", {"date": expected_as_of})
        for view in views:
            response = get(f"/api/v2/views/{view}", {"date": expected_as_of, "slot": slot} if slot != "full" else {"date": expected_as_of})
            view_payload = response.json()
            if isinstance(view_payload, dict) and view_payload.get("asOf") not in {
                None,
                expected_as_of,
            }:
                raise RuntimeError(f"{view} asOf mismatch on iteration {iteration}")
        if slot in {"full", "etf_penetration"}:
            get("/api/v2/stock-search", {"date": expected_as_of, "slot": slot} if slot != "full" else {"date": expected_as_of})
            shard = get("/api/v2/stocks/stock_0", {"date": expected_as_of, "slot": slot} if slot != "full" else {"date": expected_as_of})
            etag = shard.headers.get("etag")
            if etag:
                request_count += 1
                not_modified = requests.get(
                    f"{root}/api/v2/stocks/stock_0",
                    params={"date": expected_as_of, "slot": slot} if slot != "full" else {"date": expected_as_of},
                    headers={**headers, "If-None-Match": etag},
                    timeout=timeout,
                    allow_redirects=False,
                )
                if not_modified.status_code not in {200, 304}:
                    not_modified.raise_for_status()
    return {
        "releaseId": expected_release_id,
        "asOf": expected_as_of,
        "buildSha": latest.get("buildSha"),
        "slot": slot,
        "checkedViews": len(views),
        "iterations": repeat,
        "requests": request_count,
    }


def verify_candidate(
    base_url: str,
    *,
    expected_release_id: str,
    expected_as_of: str,
    expected_build_sha: str | None = None,
    timeout: float = 20.0,
    slot: str = "full",
) -> dict[str, Any]:
    """Verify a staged release by explicit release ID without reading active routes."""

    _views, artifacts = slot_contract(slot)
    root = base_url.rstrip("/")
    headers = _candidate_headers()
    for name in artifacts:
        response = requests.get(
            f"{root}/api/v1/admin/releases/artifact",
            params={"releaseId": expected_release_id, "name": name, "candidate": "1"},
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.headers.get("x-flow-release-id") != expected_release_id:
            raise RuntimeError(f"candidate artifact {name} selected another release")
        if response.headers.get("x-flow-as-of") != expected_as_of:
            raise RuntimeError(f"candidate artifact {name} has an unexpected asOf")
        if expected_build_sha and response.headers.get("x-flow-build-sha") != expected_build_sha:
            raise RuntimeError(f"candidate artifact {name} has an unexpected build SHA")
        if response.headers.get("x-flow-release-status") != "STAGED":
            raise RuntimeError(f"candidate artifact {name} is not STAGED")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("asOf") not in {None, expected_as_of}:
            raise RuntimeError(f"candidate artifact {name} has an unexpected payload date")
    return {
        "releaseId": expected_release_id,
        "asOf": expected_as_of,
        "mode": "candidate",
        "slot": slot,
        "buildSha": expected_build_sha,
        "checkedArtifacts": len(artifacts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an immutable Flow read-model release")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Repeat the active-release read canary 1-100 times",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--build-sha", default=os.environ.get("FLOW_EXPECTED_BUILD_SHA"))
    parser.add_argument(
        "--slot",
        choices=("dashboard", "etf_penetration", "full"),
        default="full",
        help="Release slot to verify",
    )
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="Verify an explicit STAGED candidate via the admin artifact endpoint; never reads active routes",
    )
    args = parser.parse_args()
    verifier = verify_candidate if args.candidate else verify
    print(
        json.dumps(
            verifier(
                args.base_url,
                expected_release_id=args.release_id,
                expected_as_of=args.as_of,
                expected_build_sha=args.build_sha,
                repeat=args.repeat,
                timeout=args.timeout,
                slot=args.slot,
            )
        )
    )


if __name__ == "__main__":
    main()
