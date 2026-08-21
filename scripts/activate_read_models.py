"""Promote one staged read-model candidate after its explicit canary passes."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import requests


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def activate(
    base_url: str,
    *,
    release_id: str,
    as_of: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    root = base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {required_env('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{root}/api/v1/admin/read-model-activate",
        headers=headers,
        json={"releaseId": release_id, "asOf": as_of},
        timeout=timeout,
        allow_redirects=False,
    )
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"candidate activation failed ({response.status_code}): {detail}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("releaseId") != release_id or payload.get("asOf") != as_of:
        raise RuntimeError(f"candidate activation response identity mismatch: {payload}")
    if payload.get("status") not in {"activated", "staged_older_than_active", "staged_stale_revision"}:
        raise RuntimeError(f"candidate activation returned an unsafe status: {payload}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Activate one canary-verified read-model candidate")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            print(json.dumps(activate(args.base_url, release_id=args.release_id, as_of=args.as_of, timeout=args.timeout)))
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"candidate activation failed after retries: {last_error}")


if __name__ == "__main__":
    main()
