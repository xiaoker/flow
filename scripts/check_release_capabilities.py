"""Fail-closed preflight for the deployed read-model release contract."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def check(url: str, *, timeout: float = 20.0, slot: str = "full") -> dict:
    if slot not in {"dashboard", "etf_penetration", "full"}:
        raise ValueError(f"unsupported release slot: {slot}")
    headers = {
        "Authorization": f"Bearer {_required('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": _required("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": _required("FLOW_CF_ACCESS_CLIENT_SECRET"),
    }
    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("capabilities response is not an object")
    expected = {
        "readModelManifestSchemas": 3,
        "schemaVersions": 3,
        "pressureSchemaVersions": 2,
        "stockShardCounts": 8,
    }
    for key, value in expected.items():
        values = payload.get(key)
        if not isinstance(values, list) or value not in values:
            raise RuntimeError(f"deployed capability missing {key}={value}: {payload}")
    if payload.get("maxArtifacts", 0) < 20 or payload.get("maxArtifactBytes", 0) < 5_000_000:
        raise RuntimeError(f"deployed artifact budget is too small: {payload}")
    features = set(payload.get("features") or [])
    required_features = {
        "requiredArtifacts",
        "sameDateObservedAtOrder",
        "releaseSlots",
        "bseMarginSeparateScope",
    }
    if slot in {"etf_penetration", "full"}:
        required_features.update({"previousCompatibleRelease", "radarFlowView", "sourceReadinessLedger"})
    if not required_features.issubset(features):
        raise RuntimeError(f"deployed release features are incomplete: {payload}")
    migration = payload.get("migration")
    if not isinstance(migration, dict) or migration.get("releaseObservedAtColumns") is not True:
        raise RuntimeError(f"release observed_at migration is not ready: {payload}")
    if migration.get("marketHistoryBseColumn") is not True:
        raise RuntimeError(f"BSE history migration is not ready: {payload}")
    if slot in {"etf_penetration", "full"} and migration.get("sourceReadinessLedger") is not True:
        raise RuntimeError(f"source readiness migration is not ready: {payload}")
    if payload.get("marketHistoryScopeVersion") != "sse-szse-bse-v1":
        raise RuntimeError(f"market history scope is not three-market: {payload}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Check deployed Flow read-model capabilities")
    parser.add_argument("--url", default="https://flow.xiaoker.com/api/v1/admin/capabilities")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--slot",
        choices=("dashboard", "etf_penetration", "full"),
        default="full",
        help="Capability contract to check; dashboard does not require PCF readiness",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = check(args.url, timeout=args.timeout, slot=args.slot)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
