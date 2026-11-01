"""Promote one staged read-model candidate after its explicit canary passes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
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
    slot: str = "dashboard",
    timeout: float = 30.0,
    metrics: dict[str, int] | None = None,
    publish_history: bool = False,
) -> dict[str, Any]:
    if type(publish_history) is not bool:
        raise ValueError("invalid_history_publication_intent")
    root = base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {required_env('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
        "Content-Type": "application/json",
    }
    if metrics is not None:
        metrics["workerRequests"] = metrics.get("workerRequests", 0) + 1
    response = requests.post(
        f"{root}/api/v1/admin/read-model-activate",
        headers=headers,
        json={"releaseId": release_id, "asOf": as_of, "slot": slot,
              **({"publishHistory": True} if publish_history else {})},
        timeout=timeout,
        allow_redirects=False,
    )
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise RuntimeError(f"candidate activation failed ({response.status_code}): {detail}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("releaseId") != release_id or payload.get("asOf") != as_of or payload.get("slot", "dashboard") != slot:
        raise RuntimeError(f"candidate activation response identity mismatch: {payload}")
    allowed = {"activated", "staged_older_than_active", "staged_stale_revision"}
    if publish_history:
        allowed.add("published_historical")
    if payload.get("status") not in allowed:
        raise RuntimeError(f"candidate activation returned an unsafe status: {payload}")
    return payload


def _published_date_paths(manifest: dict[str, Any]):
    release_id, as_of, slot = manifest.get("releaseId"), manifest.get("asOf"), manifest.get("slot", "dashboard")
    artifacts = manifest.get("artifacts")
    if slot not in {"dashboard", "etf_penetration"} or not isinstance(release_id, str) or not isinstance(as_of, str):
        raise ValueError("published_date_identity_missing")
    if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 20:
        raise ValueError("published_date_artifact_envelope_invalid")
    paths = {}
    total = 0
    for name, descriptor in artifacts.items():
        if name in {"bootstrap", "overview", "radar", "radar_flow", "rotation", "national", "connect", "margin", "pressure"}:
            paths[name] = f"/api/v2/views/{name}"
        elif name == "stock_search": paths[name] = "/api/v2/stock-search"
        elif re.fullmatch(r"stock_[0-7]", name): paths[name] = f"/api/v2/stocks/{name}"
        else: raise ValueError("published_date_unknown_artifact")
        if not isinstance(descriptor, dict) or type(descriptor.get("bytes")) is not int or not 0 < descriptor["bytes"] <= 5_000_000:
            raise ValueError("published_date_artifact_size_invalid")
        if not isinstance(descriptor.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", descriptor["sha256"]):
            raise ValueError("published_date_artifact_hash_invalid")
        total += descriptor["bytes"]
    if total > 32_000_000: raise ValueError("published_date_total_size_exceeded")
    return slot, artifacts, paths


def verify_published_date(base_url: str, *, manifest: dict[str, Any], active_release_id: str,
                          timeout: float = 30, metrics: dict[str, int] | None = None) -> dict[str, Any]:
    """Normal (not candidate/admin) reads with exact body hashes and stable latest."""
    slot, artifacts, paths = _published_date_paths(manifest)
    release_id, as_of = manifest["releaseId"], manifest["asOf"]
    if not isinstance(active_release_id, str) or not active_release_id:
        raise ValueError("published_date_identity_missing")
    headers = {"CF-Access-Client-Id": required_env("FLOW_CF_ACCESS_CLIENT_ID"),
               "CF-Access-Client-Secret": required_env("FLOW_CF_ACCESS_CLIENT_SECRET")}
    count = 0
    def get(path: str, params: dict[str, str], limit: int, *, artifact: bool = False):
        nonlocal count
        count += 1
        if metrics is not None:
            metrics["workerRequests"] = metrics.get("workerRequests", 0) + 1
            if artifact: metrics["r2ClassB"] = metrics.get("r2ClassB", 0) + 1
        with requests.get(base_url.rstrip("/") + path, params=params, headers=headers,
                          timeout=timeout, allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            wire = response.raw.read(limit + 1, decode_content=True)
            if len(wire) > limit: raise RuntimeError("published_date_response_size_exceeded")
            return {key.lower(): value for key, value in response.headers.items()}, wire
    def latest():
        response_headers, wire = get("/api/v2/releases/latest", {"slot": slot}, 65_536)
        value = json.loads(wire)
        if not isinstance(value, dict) or value.get("releaseId") != active_release_id:
            raise RuntimeError("published_date_active_changed")
        if response_headers.get("x-flow-release-id") != active_release_id:
            raise RuntimeError("published_date_latest_identity_missing")
        return value.get("releaseId"), value.get("asOf")
    before = latest()
    hashes = {}
    for name, descriptor in artifacts.items():
        response_headers, wire = get(paths[name], {"date": as_of, "slot": slot}, descriptor["bytes"], artifact=True)
        lowered = {key.lower(): value for key, value in response_headers.items()}
        if lowered.get("x-flow-release-id") != release_id or lowered.get("x-flow-as-of") != as_of:
            raise RuntimeError("published_date_selected_another_release")
        digest = hashlib.sha256(wire).hexdigest()
        if len(wire) != descriptor["bytes"] or digest != descriptor["sha256"]:
            raise RuntimeError("published_date_artifact_hash_mismatch")
        hashes[name] = digest
    if latest() != before: raise RuntimeError("published_date_active_changed")
    return {"status": "PASS", "mode": "published_date", "releaseId": release_id, "asOf": as_of,
            "slot": slot, "activeReleaseId": before[0], "activeAsOf": before[1],
            "artifactHashes": hashes, "requests": count}


def main() -> None:
    parser = argparse.ArgumentParser(description="Activate one canary-verified read-model candidate")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_PUBLIC_URL", "https://flow.xiaoker.com"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--slot", choices=("dashboard", "etf_penetration"), default="dashboard")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--metrics-output", type=Path, help="Optional measured Worker request counter")
    parser.add_argument("--publish-history", action="store_true", help="Explicitly publish older dates without moving latest; verify normal reads")
    parser.add_argument("--read-model-manifest", type=Path)
    parser.add_argument("--readback-output", type=Path)
    args = parser.parse_args()
    manifest = None
    if args.publish_history:
        if not args.read_model_manifest or not args.readback_output:
            parser.error("--publish-history requires --read-model-manifest and --readback-output")
        manifest = json.loads(args.read_model_manifest.read_bytes())
        if (manifest.get("releaseId"), manifest.get("asOf"), manifest.get("slot", "dashboard")) != (args.release_id, args.as_of, args.slot):
            raise ValueError("history_manifest_identity_mismatch")
        _published_date_paths(manifest)  # Reject invalid local input before the first write.
    last_error: Exception | None = None
    metrics: dict[str, int] = {"workerRequests": 0, "r2Bytes": 0, "r2ClassA": 0, "r2ClassB": 0}
    for attempt in range(1 if args.publish_history else 3):
        try:
            result = activate(
                args.base_url,
                release_id=args.release_id,
                as_of=args.as_of,
                slot=args.slot,
                timeout=args.timeout,
                metrics=metrics,
                publish_history=args.publish_history,
            )
            if args.publish_history:
                if result.get("status") not in {"activated", "published_historical"}:
                    raise RuntimeError("recovery_publication_not_readable")
                proof = verify_published_date(args.base_url, manifest=manifest,
                    active_release_id=result.get("activeReleaseId"), timeout=args.timeout, metrics=metrics)
                args.readback_output.parent.mkdir(parents=True, exist_ok=True)
                args.readback_output.write_text(json.dumps(proof, sort_keys=True) + "\n")
            if args.metrics_output:
                args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
                args.metrics_output.write_text(json.dumps(metrics, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps(result))
            return
        except Exception as exc:
            last_error = exc
            if args.metrics_output:
                args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
                args.metrics_output.write_text(json.dumps(metrics) + "\n")
            if args.publish_history: break  # Never replay an ambiguous write/readback as another attempt.
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"candidate activation failed after retries: {last_error}")


if __name__ == "__main__":
    main()
