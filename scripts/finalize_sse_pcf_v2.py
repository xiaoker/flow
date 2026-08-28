#!/usr/bin/env python3
"""Validate captured SSE PCF XML and publish immutable v2 canonical artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from scripts.capture_sse_pcf_v2 import (
    CaptureError,
    RateLimited,
    _auth_headers,
    _required_items,
    capture_status,
    submit_plan,
)
from trading_desk.pcf_canonical import build_pcf_canonical_artifacts
from trading_desk.pcf_xml import hash_normalized_pcf_rows, parse_sse_etf_pcf_xml


MAX_WIRE_BYTES = 5 * 1024 * 1024


def _json_response(response: requests.Response, *, label: str) -> dict[str, Any]:
    if response.status_code in {401, 403, 429}:
        raise RateLimited(f"{label}_http_{response.status_code}")
    if not response.ok:
        raise CaptureError(f"{label}_http_{response.status_code}:{response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CaptureError(f"{label}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise CaptureError(f"{label}_invalid_response")
    return payload


def download_raw_xml(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    job_id: str,
    code: str,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    response = session.get(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture/object",
        params={"jobId": job_id, "etfCode": code},
        headers=_auth_headers(
            api_key,
            access_client_id=access_client_id,
            access_client_secret=access_client_secret,
        ),
        timeout=(10.0, 90.0),
    )
    if response.status_code in {401, 403, 429}:
        raise RateLimited(f"{code}_download_http_{response.status_code}")
    if not response.ok:
        raise CaptureError(f"{code}_download_http_{response.status_code}")
    wire = response.content
    if not wire or len(wire) > MAX_WIRE_BYTES:
        raise CaptureError(f"{code}_download_size_invalid")
    object_sha = response.headers.get("X-Flow-Object-Sha256", "").lower()
    raw_sha = response.headers.get("X-Flow-Raw-Sha256", "").lower()
    if hashlib.sha256(wire).hexdigest() != object_sha:
        raise CaptureError(f"{code}_object_sha256_mismatch")
    try:
        raw = gzip.decompress(wire)
    except (OSError, EOFError) as exc:
        raise CaptureError(f"{code}_gzip_invalid") from exc
    if hashlib.sha256(raw).hexdigest() != raw_sha:
        raise CaptureError(f"{code}_raw_sha256_mismatch")
    return raw, {
        "objectSha256": object_sha,
        "rawSha256": raw_sha,
        "tradingDay": response.headers.get("X-Flow-Source-Trading-Day", ""),
        "preTradingDay": response.headers.get("X-Flow-Source-Previous-Day", ""),
        "observedAt": response.headers.get("X-Flow-Observed-At", ""),
        "normalizedSha256": response.headers.get("X-Flow-Normalized-Sha256", "").lower(),
    }


def upload_artifact(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    key: str,
    sha256: str,
    content: bytes,
    content_type: str,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> dict[str, Any]:
    if hashlib.sha256(content).hexdigest() != sha256:
        raise CaptureError("canonical_artifact_local_sha256_mismatch")
    response = session.put(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture/artifact",
        headers={
            **_auth_headers(
                api_key,
                access_client_id=access_client_id,
                access_client_secret=access_client_secret,
            ),
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "X-Flow-Artifact-Key": key,
            "X-Flow-Artifact-Sha256": sha256,
        },
        data=content,
        timeout=(10.0, 120.0),
    )
    return _json_response(response, label="canonical_artifact_upload")


def finalize_remote(
    session: requests.Session,
    *,
    base_url: str,
    api_key: str,
    build: Mapping[str, Any],
    validated_items: list[dict[str, Any]],
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> dict[str, Any]:
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/admin/pcf-capture/finalize",
        headers={
            **_auth_headers(
                api_key,
                access_client_id=access_client_id,
                access_client_secret=access_client_secret,
            ),
            "Content-Type": "application/json",
        },
        json={
            "revisionId": build["revisionId"],
            "manifestKey": build["manifestKey"],
            "manifestSha256": build["manifestSha256"],
            "manifest": build["manifest"],
            "validatedItems": validated_items,
        },
        timeout=(10.0, 90.0),
    )
    return _json_response(response, label="canonical_finalize")


def finalize_plan(
    plan: Mapping[str, Any],
    *,
    base_url: str,
    api_key: str,
    session: requests.Session | None = None,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> dict[str, Any]:
    client = session or requests.Session()
    target_date = str(plan.get("targetDate") or "")
    previous_date = str(plan.get("previousDate") or "")
    job_response = submit_plan(
        client,
        base_url=base_url,
        api_key=api_key,
        plan=plan,
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    job_id = str(job_response.get("jobId") or "")
    if not job_id:
        raise CaptureError("control_plane_job_id_missing")
    if plan.get("status") in {"BLOCKED_QUALITY", "BLOCKED_UNIVERSE_ANOMALY"}:
        return {
            "schemaVersion": 1,
            "shadowOnly": True,
            "status": str(plan["status"]),
            "tradeDate": target_date,
            "jobId": job_id,
            "coverage": {"required": 0, "captured": 0, "valid": 0, "failed": 0},
        }

    required = _required_items(plan)
    required_by_code = {str(item["etfCode"]): item for item in required}
    status = capture_status(
        client,
        base_url=base_url,
        api_key=api_key,
        job_id=job_id,
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    item_states = {
        str(item.get("etfCode")): str(item.get("state"))
        for item in status.get("items", [])
        if isinstance(item, dict)
    }
    unavailable = sorted(code for code in required_by_code if item_states.get(code) not in {"CAPTURED", "VALID"})
    if unavailable:
        return {
            "schemaVersion": 1,
            "shadowOnly": True,
            "status": "PARTIAL",
            "tradeDate": target_date,
            "previousDate": previous_date,
            "jobId": job_id,
            "coverage": {
                "required": len(required),
                "captured": len(required) - len(unavailable),
                "valid": 0,
                "failed": len(unavailable),
            },
            "unavailableCodes": unavailable[:100],
        }

    raw_xml: dict[str, bytes] = {}
    raw_metadata: dict[str, dict[str, Any]] = {}
    headers: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    data: list[dict[str, Any]] = []
    validated_items: list[dict[str, Any]] = []
    observed_values: list[str] = []
    for code in sorted(required_by_code):
        raw, lineage = download_raw_xml(
            client,
            base_url=base_url,
            api_key=api_key,
            job_id=job_id,
            code=code,
            access_client_id=access_client_id,
            access_client_secret=access_client_secret,
        )
        rows, parsed_header, warnings = parse_sse_etf_pcf_xml(
            raw,
            requested_code=code,
            target_date=target_date,
            previous_date=previous_date,
        )
        if warnings:
            raise CaptureError(f"{code}_exact_date_contract_failed:{';'.join(warnings)}")
        observed_at = lineage.get("observedAt") or ""
        if observed_at:
            observed_values.append(observed_at)
        etf_name = str(parsed_header.get("fundName") or required_by_code[code].get("etfName") or code)
        header_row = {
            "market": "SSE",
            "etf_code": code,
            "etf_name": etf_name,
            "fund_instrument_id": parsed_header.get("fundInstrumentId"),
            "trading_day": parsed_header.get("tradingDay"),
            "pre_trading_day": parsed_header.get("preTradingDay"),
            "creation_redemption_unit": parsed_header.get("creationRedemptionUnit"),
            "record_number": parsed_header.get("recordNumber"),
            "source_extra_json": parsed_header.get("sourceExtraJson", "{}"),
        }
        headers.append(header_row)
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            component = {**row, "etf_name": etf_name}
            components.append(component)
            normalized = {
                "trade_date": target_date,
                "pcf_date": target_date,
                "pcf_previous_date": previous_date,
                **component,
                "source_id": "sse",
                "source_grade": "A",
                "date_basis": "sse_pcf_exact_same_trading_day_shadow",
                "observed_at": observed_at,
            }
            normalized_rows.append(normalized)
            data.append(normalized)
        normalized_sha = hash_normalized_pcf_rows(rows)
        if lineage.get("normalizedSha256") != normalized_sha:
            raise CaptureError(f"{code}_normalized_sha256_mismatch")
        raw_xml[code] = raw
        raw_metadata[code] = lineage
        validated_items.append(
            {
                "etfCode": code,
                "tradingDay": target_date,
                "preTradingDay": previous_date,
                "componentCount": len(rows),
                "normalizedSha256": normalized_sha,
            }
        )

    status_job = status.get("job") if isinstance(status.get("job"), Mapping) else {}
    observed_at = max(observed_values) if observed_values else str(status_job.get("started_at") or "") or None
    counts = plan.get("counts") if isinstance(plan.get("counts"), Mapping) else {}
    build = build_pcf_canonical_artifacts(
        trade_date=target_date,
        previous_date=previous_date,
        job_id=job_id,
        plan_sha256=str(plan.get("planSha256") or ""),
        headers=headers,
        components=components,
        data=data,
        raw_xml=raw_xml,
        raw_metadata=raw_metadata,
        required_count=len(required),
        captured_count=len(required),
        valid_count=len(required),
        failed_count=0,
        unchanged_count=int(counts.get("unchanged", 0)),
        new_baseline_missing=int(counts.get("newBaselineMissing", 0)),
        missing_current=int(counts.get("missingCurrent", 0)),
        quality={"pass": len(required), "warn": 0, "fail": 0},
        observed_at=observed_at,
    )
    if not build["ready"]:
        raise CaptureError("canonical_not_ready")
    uploaded: list[dict[str, Any]] = []
    for name in ("rawPack", "headers", "components", "data"):
        artifact = build["artifacts"][name]
        result = upload_artifact(
            client,
            base_url=base_url,
            api_key=api_key,
            key=artifact["key"],
            sha256=artifact["sha256"],
            content=artifact["bytes"],
            content_type="application/gzip",
            access_client_id=access_client_id,
            access_client_secret=access_client_secret,
        )
        uploaded.append({"name": name, "key": artifact["key"], "sha256": artifact["sha256"], **result})
    upload_artifact(
        client,
        base_url=base_url,
        api_key=api_key,
        key=build["manifestKey"],
        sha256=build["manifestSha256"],
        content=build["manifestBytes"],
        content_type="application/json",
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    remote = finalize_remote(
        client,
        base_url=base_url,
        api_key=api_key,
        build=build,
        validated_items=validated_items,
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    return {
        "schemaVersion": 1,
        "shadowOnly": True,
        "status": str(remote.get("status") or "READY"),
        "tradeDate": target_date,
        "previousDate": previous_date,
        "jobId": job_id,
        "revisionId": build["revisionId"],
        "manifestKey": build["manifestKey"],
        "manifestSha256": build["manifestSha256"],
        "coverage": {"required": len(required), "captured": len(required), "valid": len(required), "failed": 0},
        "budget": {
            "sourceRequests": 0,
            "r2ClassA": len(required) + 5,
            "r2ClassB": len(required),
            "d1RowsReadUpperBound": len(required) * 3 + 20,
            "d1RowsWrittenUpperBound": len(required) + 8,
        },
        "artifacts": uploaded,
        "productionPublishCalls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--base-url", default="https://flow.xiaoker.com")
    parser.add_argument("--api-key", default=os.environ.get("FLOW_INGEST_API_KEY", ""))
    parser.add_argument("--access-client-id", default=os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", ""))
    parser.add_argument("--access-client-secret", default=os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", ""))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise SystemExit("plan must be a JSON object")
    try:
        result = finalize_plan(
            plan,
            base_url=args.base_url,
            api_key=args.api_key,
            access_client_id=args.access_client_id or None,
            access_client_secret=args.access_client_secret or None,
        )
    except (CaptureError, RateLimited, requests.RequestException, ValueError) as exc:
        result = {
            "schemaVersion": 1,
            "shadowOnly": True,
            "status": "ERROR",
            "tradeDate": str(plan.get("targetDate") or ""),
            "error": f"{type(exc).__name__}:{exc}",
            "productionPublishCalls": 0,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"READY", "READY_NO_CHANGE"} else 2


if __name__ == "__main__":
    sys.exit(main())
