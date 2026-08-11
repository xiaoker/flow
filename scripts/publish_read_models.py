from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = requests.post(url, data=encoded, headers=headers, timeout=(10, 90))
            if response.ok:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("read model import did not return an object")
                return result
            detail = response.text[:500].replace("\n", " ")
            last_error = RuntimeError(f"{response.status_code} response: {detail}")
            if response.status_code not in {408, 429} and response.status_code < 500:
                break
        except Exception as exc:  # pragma: no cover - exercised by live transport
            last_error = exc
        if attempt < 3:
            time.sleep(2**attempt)
    raise RuntimeError(f"read model import failed after retries: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish read models one artifact per Worker request")
    parser.add_argument("--read-model-dir", type=Path, default=Path("read-models"))
    parser.add_argument("--url", default="https://flow.xiaoker.com/api/v1/admin/read-model-import")
    args = parser.parse_args()

    manifest = json.loads((args.read_model_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
        raise ValueError("read-model manifest is invalid")
    headers = {
        "Authorization": f"Bearer {required_env('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
        "Content-Type": "application/json",
    }
    names = list(manifest["artifacts"])
    if not names:
        raise ValueError("read-model manifest has no artifacts")
    for index, name in enumerate(names):
        declared = manifest["artifacts"][name]
        content = (args.read_model_dir / f"{name}.json").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != int(declared["bytes"]) or digest != declared["sha256"]:
            raise ValueError(f"read-model digest mismatch: {name}")
        wire_content = gzip.compress(content, compresslevel=6)
        body = {
            "version": 1,
            "manifest": manifest,
            "artifact": {
                "name": name,
                "key": declared["key"],
                "sha256": digest,
                "bytes": len(content),
                "contentBase64": base64.b64encode(wire_content).decode("ascii"),
                "contentEncoding": "gzip",
            },
            "finalize": index == len(names) - 1,
        }
        result = post(args.url, body, headers)
        print(json.dumps({"name": name, **result}, ensure_ascii=False))
        if index == len(names) - 1 and result.get("status") not in {"activated", "staged_older_than_active"}:
            raise RuntimeError(f"read model release was not activated: {result}")


if __name__ == "__main__":
    main()
