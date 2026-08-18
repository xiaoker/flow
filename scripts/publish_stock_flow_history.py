"""Upload ETF-to-stock daily partitions as immutable R2 checkpoints."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests


def _post(url: str, payload: dict, headers: dict[str, str], attempts: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.post(url, data=body, headers=headers, timeout=(10, 90))
            if response.ok:
                result = response.json()
                if not isinstance(result, dict):
                    raise RuntimeError("partition import response is not an object")
                return result
            last = RuntimeError(f"{response.status_code}: {response.text[:300]}")
        except Exception as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(2**attempt)
    raise RuntimeError(f"partition import failed after retries: {last}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish ETF-to-stock daily partitions")
    parser.add_argument("--partition-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--url", default="https://flow.xiaoker.com/api/v1/admin/stock-flow-history")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.attempts <= 0:
        raise ValueError("attempts must be positive")
    key = os.environ.get("FLOW_INGEST_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FLOW_INGEST_API_KEY is required")
    headers = {
        "Authorization": f"Bearer {key}",
        "CF-Access-Client-Id": os.environ.get("FLOW_CF_ACCESS_CLIENT_ID", ""),
        "CF-Access-Client-Secret": os.environ.get("FLOW_CF_ACCESS_CLIENT_SECRET", ""),
        "Content-Type": "application/json",
    }
    index = json.loads(args.index.read_text(encoding="utf-8"))
    partitions = index.get("partitions") if isinstance(index, dict) else None
    if not isinstance(partitions, list) or not partitions:
        raise RuntimeError("stock-flow history contains no available partitions")
    results = []
    for partition in partitions:
        as_of = str(partition.get("asOf") or "")
        path = args.partition_dir / f"{as_of}.json"
        if not path.is_file():
            raise RuntimeError(f"missing partition file: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = _post(args.url, {"partition": payload}, headers, args.attempts)
        results.append({"asOf": as_of, "status": result.get("status"), "sha256": result.get("sha256")})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    print(json.dumps({"status": "complete", "partitions": len(results), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
