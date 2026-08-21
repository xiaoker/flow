#!/usr/bin/env python3
"""Resumable, rate-limited SSE PCF v2 shadow collector.

The script is intentionally not wired into the production workflow until the
dual-run PR.  It creates a control-plane job first, then fetches only required
changed ETFs one at a time and uploads each gzip object to the streaming admin
endpoint.  The endpoint updates the D1 item checkpoint, so a later run skips
objects already acknowledged by R2/D1.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import requests


SSE_XML_URL = "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do"
XML_CONTENT_TYPES = {"application/xml", "text/xml"}
CODE_RE = re.compile(r"^\d{6}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RateLimited(RuntimeError):
    """Raised when SSE signals that the current window must be stopped."""


class CaptureError(RuntimeError):
    """Raised for a non-retryable per-item capture error."""


def _json_response(response: requests.Response, *, label: str) -> dict[str, Any]:
    if response.status_code not in {200, 201, 202, 204}:
        raise CaptureError(f"{label}_http_{response.status_code}")
    try:
        value = response.json()
    except ValueError as exc:
        raise CaptureError(f"{label}_invalid_json") from exc
    if not isinstance(value, dict):
        raise CaptureError(f"{label}_invalid_response")
    return value


def _is_xml_response(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in XML_CONTENT_TYPES:
        return True
    disposition = response.headers.get("Content-Disposition", "")
    return (
        content_type == "application/octet-stream"
        and re.search(r"\.xml(?:[\"']|$)", disposition, flags=re.IGNORECASE) is not None
        and response.content.lstrip()[:5].upper().startswith(b"<?XML")
    )


def _required_items(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = plan.get("items")
    if not isinstance(items, list):
        raise CaptureError("plan_items_missing")
    result = [item for item in items if isinstance(item, dict) and item.get("required") is True]
    for item in result:
        code = str(item.get("etfCode") or "")
        if not CODE_RE.fullmatch(code):
            raise CaptureError("plan_etf_code_invalid")
    return result


def submit_plan(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    plan: Mapping[str, Any],
    timeout: tuple[float, float] = (10.0, 45.0),
) -> dict[str, Any]:
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=plan,
        timeout=timeout,
    )
    if response.status_code in {401, 403, 429}:
        raise RateLimited(f"control_plane_http_{response.status_code}")
    return _json_response(response, label="control_plane")


def capture_status(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    timeout: tuple[float, float] = (10.0, 45.0),
) -> dict[str, Any]:
    response = session.get(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture",
        params={"jobId": job_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if response.status_code in {401, 403, 429}:
        raise RateLimited(f"control_plane_status_http_{response.status_code}")
    return _json_response(response, label="control_plane_status")


def lease(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    owner: str,
    action: str,
) -> dict[str, Any]:
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture/lease",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"action": action, "jobId": job_id, "owner": owner},
        timeout=(10.0, 45.0),
    )
    if response.status_code in {401, 403, 409, 429}:
        raise RateLimited(f"control_plane_lease_http_{response.status_code}")
    return _json_response(response, label="control_plane_lease")


def fetch_xml(
    session: requests.Session,
    *,
    code: str,
    target_date: str,
    max_attempts: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch one XML with only transport retries; 403/429 never retry."""

    for attempt in range(max_attempts):
        try:
            response = session.get(
                SSE_XML_URL,
                params={"fundCode": code},
                headers={
                    "Referer": f"https://www.sse.com.cn/disclosure/fund/etflist/detail.shtml?fundid={code}",
                    "Accept": "application/xml,text/xml,application/octet-stream;q=0.9,*/*;q=0.1",
                },
                timeout=(10.0, 45.0),
            )
        except requests.RequestException as exc:
            if attempt >= max_attempts - 1:
                raise CaptureError(f"{code}_network_{type(exc).__name__}") from exc
            sleep_fn((5, 15)[attempt])
            continue
        if response.status_code in {403, 429}:
            raise RateLimited(f"{code}_http_{response.status_code}")
        if response.status_code >= 500:
            if attempt >= max_attempts - 1:
                raise CaptureError(f"{code}_http_{response.status_code}")
            sleep_fn((5, 15)[attempt])
            continue
        if response.status_code != 200:
            raise CaptureError(f"{code}_http_{response.status_code}")
        if not _is_xml_response(response):
            raise CaptureError(f"{code}_content_type_invalid")
        if not response.content or len(response.content) > 5 * 1024 * 1024:
            raise CaptureError(f"{code}_body_size_invalid")
        return response.content
    raise CaptureError(f"{code}_request_exhausted")


def upload_xml(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    code: str,
    target_date: str,
    raw_xml: bytes,
    timeout: tuple[float, float] = (10.0, 45.0),
) -> dict[str, Any]:
    raw_sha256 = hashlib.sha256(raw_xml).hexdigest()
    wire = gzip.compress(raw_xml, compresslevel=6, mtime=0)
    object_sha256 = hashlib.sha256(wire).hexdigest()
    object_key = f"market-data/pcf-v2/raw-xml/{target_date}/{job_id}/{code}/{object_sha256}.xml.gz"
    response = session.put(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture/object",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/gzip",
            "Content-Length": str(len(wire)),
            "X-Flow-Job-Id": job_id,
            "X-Flow-ETF-Code": code,
            "X-Flow-Trade-Date": target_date,
            "X-Flow-Object-Key": object_key,
            "X-Flow-Object-Sha256": object_sha256,
            "X-Flow-Raw-Sha256": raw_sha256,
        },
        data=wire,
        timeout=timeout,
    )
    if response.status_code in {401, 403, 429}:
        raise RateLimited(f"{code}_upload_http_{response.status_code}")
    return _json_response(response, label=f"{code}_upload")


def capture_plan(
    plan: Mapping[str, Any],
    *,
    base_url: str,
    api_key: str,
    session: requests.Session | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[float, float], float] = random.uniform,
    no_sleep: bool = False,
) -> dict[str, Any]:
    """Submit and capture a plan, stopping the window on rate limiting."""

    target_date = str(plan.get("targetDate") or "")
    client = session or requests.Session()
    job_response = submit_plan(client, base_url=base_url, api_key=api_key, plan=plan)
    job_id = str(job_response.get("jobId") or "")
    if not job_id:
        raise CaptureError("control_plane_job_id_missing")
    if plan.get("status") in {"BLOCKED_QUALITY", "BLOCKED_UNIVERSE_ANOMALY", "READY_NO_CHANGE"}:
        return {
            "jobId": job_id,
            "targetDate": target_date,
            "required": len(_required_items(plan)) if plan.get("status") == "READY_NO_CHANGE" else 0,
            "captured": 0,
            "skipped": 0,
            "failed": 0,
            "failures": [],
            "status": plan.get("status"),
        }
    owner = f"pcf-script-{uuid.uuid4().hex[:16]}"
    lease(client, base_url=base_url, api_key=api_key, job_id=job_id, owner=owner, action="acquire")
    try:
        status_response = capture_status(client, base_url=base_url, api_key=api_key, job_id=job_id)
        completed_codes = {
            str(item.get("etfCode"))
            for item in status_response.get("items", [])
            if isinstance(item, dict) and item.get("state") in {"CAPTURED", "VALID"}
        }
        completed = 0
        skipped = 0
        failures: list[dict[str, str]] = []
        required = _required_items(plan)
        for index, item in enumerate(required):
            code = str(item["etfCode"])
            if code in completed_codes:
                skipped += 1
                continue
            if completed + skipped and not no_sleep:
                sleep_fn(random_fn(0.8, 1.3))
            try:
                raw_xml = fetch_xml(client, code=code, target_date=target_date, sleep_fn=sleep_fn)
                upload_xml(
                    client,
                    base_url=base_url,
                    api_key=api_key,
                    job_id=job_id,
                    code=code,
                    target_date=target_date,
                    raw_xml=raw_xml,
                )
                completed += 1
            except RateLimited:
                raise
            except CaptureError as exc:
                failures.append({"code": code, "reason": str(exc)})
        return {
            "jobId": job_id,
            "targetDate": target_date,
            "required": len(required),
            "captured": completed,
            "skipped": skipped,
            "failed": len(failures),
            "failures": failures,
        }
    finally:
        try:
            lease(client, base_url=base_url, api_key=api_key, job_id=job_id, owner=owner, action="release")
        except (RateLimited, CaptureError, requests.RequestException):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--no-sleep", action="store_true", help="testing only; do not use for production collection")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise SystemExit("plan must be a JSON object")
    try:
        result = capture_plan(
            plan,
            base_url=args.base_url,
            api_key=args.api_key,
            no_sleep=args.no_sleep,
        )
    except RateLimited as exc:
        print(json.dumps({"status": "RATE_LIMITED", "reason": str(exc)}, ensure_ascii=False))
        return 75
    except (CaptureError, requests.RequestException) as exc:
        print(json.dumps({"status": "FAILED", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
