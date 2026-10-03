from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from trading_desk.pipeline_application import retry_bounded
from trading_desk.pipeline_publication import (
    ACTIVATION_MODES, build_upload_body, validate_publication_capabilities,
)


MAX_REQUEST_BYTES = 1_500_000


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def post(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    metrics: dict[str, int] | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    class _RetryableResponse(RuntimeError):
        pass

    class _PermanentResponse(RuntimeError):
        pass

    def attempt() -> dict[str, Any]:
        if metrics is not None:
            metrics["workerRequests"] = metrics.get("workerRequests", 0) + 1
        try:
            response = requests.post(url, data=encoded, headers=headers, timeout=(10, 90))
            if response.ok:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("read model import did not return an object")
                return result
            detail = response.text[:500].replace("\n", " ")
            error = RuntimeError(f"{response.status_code} response: {detail}")
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise _RetryableResponse(str(error))
            raise _PermanentResponse(str(error))
        except (_RetryableResponse, _PermanentResponse):
            raise
        except Exception:
            # Preserve the old transport contract: unexpected network/JSON
            # errors are retryable within the same bounded attempt budget.
            raise

    def retry_exception(exc: BaseException) -> bool:
        return not isinstance(exc, _PermanentResponse)

    try:
        return retry_bounded(
            attempt,
            max_attempts=4,
            should_retry_exception=retry_exception,
            sleep=time.sleep,
        )
    except (_RetryableResponse, _PermanentResponse) as exc:
        raise RuntimeError(f"read model import failed after retries: {exc}") from exc
    except Exception as exc:  # pragma: no cover - exercised by live transport
        raise RuntimeError(f"read model import failed after retries: {exc}") from exc


def check_capabilities(
    url: str,
    headers: dict[str, str],
    *,
    slot: str = "full",
    required_manifest_schema: int = 3,
    metrics: dict[str, int] | None = None,
) -> None:
    if slot not in {"dashboard", "etf_penetration", "full"}:
        raise ValueError(f"unsupported release slot: {slot}")
    if metrics is not None:
        metrics["workerRequests"] = metrics.get("workerRequests", 0) + 1
    response = requests.get(
        url,
        headers={key: value for key, value in headers.items() if key != "Content-Type"},
        timeout=(10, 20),
        allow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    validate_publication_capabilities(payload, slot=slot,
                                      required_manifest_schema=required_manifest_schema)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish read models one artifact per Worker request")
    parser.add_argument("--read-model-dir", type=Path, default=Path("read-models"))
    parser.add_argument("--url", default="https://flow.xiaoker.com/api/v1/admin/read-model-import")
    parser.add_argument("--capabilities-url", default="https://flow.xiaoker.com/api/v1/admin/capabilities")
    parser.add_argument("--result-output", type=Path)
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Upload immutable candidate artifacts without changing the active release pointer",
    )
    parser.add_argument(
        "--slot",
        choices=("dashboard", "etf_penetration", "full"),
        help="Release slot to validate; omitted uses the manifest slot or the legacy full contract",
    )
    parser.add_argument(
        "--metrics-output",
        type=Path,
        help="Optional measured Worker request/R2 byte counters for recovery receipts",
    )
    args = parser.parse_args()
    metrics: dict[str, int] = {"workerRequests": 0, "r2Bytes": 0, "r2ClassA": 0, "r2ClassB": 0}

    manifest = json.loads((args.read_model_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
        raise ValueError("read-model manifest is invalid")
    manifest_slot = manifest.get("slot") if manifest.get("slot") in {"dashboard", "etf_penetration"} else "full"
    effective_slot = args.slot or manifest_slot
    if manifest.get("schemaVersion") == 4 and not args.stage_only:
        raise ValueError("schema-v4 capability manifests are candidate-only and require --stage-only")
    if manifest.get("schemaVersion") not in {3, 4}:
        raise ValueError("publisher requires schemaVersion 3 or 4")
    if args.slot and args.slot != manifest_slot:
        raise ValueError(f"manifest slot {manifest_slot} does not match requested slot {args.slot}")
    headers = {
        "Authorization": f"Bearer {required_env('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
        "Content-Type": "application/json",
    }
    check_capabilities(
        args.capabilities_url,
        headers,
        slot=effective_slot,
        required_manifest_schema=int(manifest["schemaVersion"]),
        metrics=metrics,
    )
    names = list(manifest["artifacts"])
    if not names:
        raise ValueError("read-model manifest has no artifacts")
    final_result: dict[str, Any] | None = None
    activation_mode = "stage_only" if args.stage_only else "active"
    for index, name in enumerate(names):
        declared = manifest["artifacts"][name]
        content = (args.read_model_dir / f"{name}.json").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != int(declared["bytes"]) or digest != declared["sha256"]:
            raise ValueError(f"read-model digest mismatch: {name}")
        body = build_upload_body(
            manifest,
            name,
            declared,
            content,
            finalize=index == len(names) - 1,
            activation_mode=activation_mode,
        )
        wire_content = gzip.compress(content, compresslevel=6)
        request_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        print(
            json.dumps(
                {
                    "name": name,
                    "rawBytes": len(content),
                    "gzipBytes": len(wire_content),
                    "requestBytes": request_bytes,
                },
                ensure_ascii=False,
            )
        )
        if request_bytes >= MAX_REQUEST_BYTES:
            raise RuntimeError(
                f"read model upload exceeds free Worker request budget: {name} {request_bytes} >= {MAX_REQUEST_BYTES}"
            )
        result = post(args.url, body, headers, metrics=metrics)
        metrics["r2Bytes"] += len(content)
        metrics["r2ClassA"] += 1
        print(json.dumps({"name": name, **result}, ensure_ascii=False))
        if index == len(names) - 1:
            final_result = result
        if index == len(names) - 1:
            allowed_statuses = {"staged_candidate"} if args.stage_only else {
                "activated",
                "staged_older_than_active",
                "staged_stale_revision",
            }
            if result.get("status") not in allowed_statuses:
                action = "staged" if args.stage_only else "activated"
                raise RuntimeError(f"read model release was not {action}: {result}")
    if args.result_output and final_result is not None:
        args.result_output.write_text(
            json.dumps(final_result, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    # The Worker writes one immutable object per artifact plus one manifest;
    # it also performs one bounded HEAD verification per artifact.  These
    # counters are deliberately emitted separately from the release response
    # so callers cannot mistake a logical row count for physical usage.
    metrics["r2ClassA"] += 1
    metrics["r2ClassB"] += len(names)
    if args.metrics_output:
        args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_output.write_text(
            json.dumps(metrics, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
