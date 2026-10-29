"""Renew one recovery lease until a bounded runner finishes.

The process is intended to run beside the live recovery command in the same
GitHub job.  It only sends the existing authenticated RUNNING status update;
it never claims a lease, dispatches a workflow, or changes a release pointer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_required('FLOW_INGEST_API_KEY')}",
        "CF-Access-Client-Id": _required("FLOW_CF_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": _required("FLOW_CF_ACCESS_CLIENT_SECRET"),
        "Content-Type": "application/json",
    }


def _write(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--lease-owner", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--interval-seconds", type=float, default=180.0)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.attempt < 0 or args.interval_seconds <= 0:
        raise ValueError("attempt must be non-negative and interval-seconds must be positive")
    deadline = datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    headers = _headers()
    session = requests.Session()
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "RUNNING",
        "renewalCount": 0,
        "measuredAt": None,
    }
    next_renewal = time.monotonic()
    try:
        while True:
            if args.stop_file and args.stop_file.exists():
                evidence["status"] = "STOPPED"
                break
            now = datetime.now(timezone.utc)
            if now >= deadline:
                evidence["status"] = "DEADLINE"
                evidence["errorCode"] = "live_recovery_deadline_expired"
                _write(args.evidence, evidence)
                return
            until_renewal = next_renewal - time.monotonic()
            if until_renewal > 0:
                # A short local stop-file poll is not a network renewal.
                remaining = (deadline - now).total_seconds()
                time.sleep(min(until_renewal, remaining, 5.0))
                continue
            payload = {
                "recoveryId": args.recovery_id,
                "runKey": args.run_key,
                "leaseOwner": args.lease_owner,
                "expectedAttemptCount": args.attempt,
                "status": "RUNNING",
                "renewLease": True,
            }
            response = session.post(args.url, json=payload, headers=headers, timeout=(10, 30))
            if not response.ok:
                evidence.update(
                    {
                        "status": "FAILED",
                        "errorCode": "live_recovery_lease_renew_failed",
                        "httpStatus": response.status_code,
                    }
                )
                _write(args.evidence, evidence)
                raise RuntimeError(f"lease renewal failed with HTTP {response.status_code}")
            evidence["renewalCount"] += 1
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                evidence.update(
                    {
                        "status": "FAILED",
                        "errorCode": "live_recovery_lease_renew_payload_invalid",
                    }
                )
                _write(args.evidence, evidence)
                raise RuntimeError("lease renewal returned a non-object payload")
            if isinstance(body, dict):
                if body.get("status") != "RUNNING" or body.get("recoveryStatus") != "ACTIVE":
                    evidence.update(
                        {
                            "status": "FAILED",
                            "errorCode": "live_recovery_lease_renew_state_invalid",
                        }
                    )
                    _write(args.evidence, evidence)
                    raise RuntimeError("lease renewal returned a non-running state")
                if str(body.get("attemptCount")) != str(args.attempt):
                    evidence.update(
                        {
                            "status": "FAILED",
                            "errorCode": "live_recovery_lease_renew_attempt_mismatch",
                        }
                    )
                    _write(args.evidence, evidence)
                    raise RuntimeError("lease renewal returned a different attempt")
                # Only copy non-sensitive control fields.
                if not isinstance(body.get("leaseExpiresAt"), str) or not body["leaseExpiresAt"]:
                    evidence.update(
                        {
                            "status": "FAILED",
                            "errorCode": "live_recovery_lease_renew_expiry_missing",
                        }
                    )
                    _write(args.evidence, evidence)
                    raise RuntimeError("lease renewal did not return an expiry")
                for key in ("status", "recoveryStatus", "leaseExpiresAt", "attemptCount", "rowsWritten", "rowsRead"):
                    if key in body:
                        evidence[key] = body[key]
            evidence["measuredAt"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
            _write(args.evidence, evidence)
            next_renewal = time.monotonic() + args.interval_seconds
    except Exception as exc:
        # Do not include response bodies or request headers in logs; they may
        # contain deployment metadata or accidentally echoed credentials.
        evidence.setdefault("status", "FAILED")
        evidence["errorCode"] = "live_recovery_lease_renew_failed"
        evidence["detail"] = type(exc).__name__
        _write(args.evidence, evidence)
        print(json.dumps({"status": evidence["status"], "errorCode": evidence["errorCode"]}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        _write(args.evidence, evidence)


if __name__ == "__main__":
    main()
