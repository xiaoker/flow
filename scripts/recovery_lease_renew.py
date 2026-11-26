"""Renew one pipeline lease until a bounded runner finishes.

The recovery runner uses the recovery-status endpoint in the foreground. An
ordinary collector/publisher may run the same renewer detached within its job;
if that lease is lost, the renewer cancels its GitHub Actions run so stale work
cannot continue to publish. Neither mode claims a lease or changes a release
pointer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


def _post_json(
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float = 30,
) -> tuple[int, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise RuntimeError("control response exceeded the size limit")
            if not raw:
                return response.status, {}
            try:
                return response.status, json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return response.status, None
    except HTTPError as error:
        # Deliberately do not parse or log an error body; gateways can echo
        # request metadata and credentials.
        diagnostic: dict[str, str] = {}
        # Only fixed-format, non-secret response metadata is retained. Never
        # retain Location, cookies, authentication headers or arbitrary values.
        for header, key, pattern in (
            ("CF-Ray", "cfRay", r"[a-fA-F0-9]{16,32}-[A-Z]{3}"),
            ("Content-Type", "contentType", r"(?:application/json|text/html)(?:;\s*charset=[A-Za-z0-9-]+)?"),
            ("cf-mitigated", "mitigation", r"challenge"),
        ):
            value = (error.headers.get(header, "") if error.headers else "").strip()
            if re.fullmatch(pattern, value, re.IGNORECASE):
                diagnostic[key] = value
        error.close()
        return error.code, {"httpDiagnostic": diagnostic} if diagnostic else {}
    except URLError as error:
        raise RuntimeError("control request transport failed") from error


def _write(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _ordinary_expiry_is_future(value: Any, now: datetime) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expiry.tzinfo is not None and expiry > now


def _validate_renewal(
    mode: str,
    body: Any,
    *,
    run_key: str,
    lease_owner: str,
    attempt: int,
    now: datetime,
) -> dict[str, Any]:
    prefix = "live_recovery" if mode == "recovery" else "pipeline_window"
    if not isinstance(body, dict):
        raise ValueError(f"{prefix}_lease_renew_payload_invalid")
    if body.get("status") != "RUNNING" or str(body.get("attemptCount")) != str(attempt):
        raise ValueError(f"{prefix}_lease_renew_state_invalid")
    if mode == "recovery":
        if body.get("recoveryStatus") != "ACTIVE":
            raise ValueError("live_recovery_lease_renew_state_invalid")
        expiry = body.get("leaseExpiresAt")
        if not isinstance(expiry, str) or not expiry:
            raise ValueError("live_recovery_lease_renew_expiry_missing")
    else:
        if body.get("runKey") != run_key or body.get("leaseOwner") != lease_owner:
            raise ValueError("pipeline_window_lease_renew_identity_mismatch")
        if body.get("recoveryId") is not None or body.get("decision") != "renewed":
            raise ValueError("pipeline_window_lease_renew_mode_mismatch")
        if not _ordinary_expiry_is_future(body.get("leaseExpiresAt"), now):
            raise ValueError("pipeline_window_lease_renew_expiry_invalid")
        rows_written = body.get("rowsWritten")
        if type(rows_written) is not int or not 1 <= rows_written <= 9_007_199_254_740_991:
            raise ValueError("pipeline_window_lease_renew_write_count_invalid")
    return body


def _cancel_ordinary_workflow(evidence: dict[str, Any]) -> str:
    try:
        repository = _required("GITHUB_REPOSITORY")
        run_id = _required("GITHUB_RUN_ID")
        token = _required("PIPELINE_LEASE_HEARTBEAT_GITHUB_TOKEN")
    except Exception as exc:
        evidence["workflowCancellation"] = "failed"
        evidence["workflowCancellationError"] = type(exc).__name__
        return "failed"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not run_id.isdigit():
        evidence["workflowCancellation"] = "failed"
        evidence["workflowCancellationError"] = "invalid_run_identity"
        return "failed"
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/cancel"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    for attempt in range(3):
        try:
            status, _ = _post_json(url, None, headers, timeout=10)
        except Exception:
            status = 0
        evidence["workflowCancellationLastHttpStatus"] = status
        if status == 202:
            evidence["workflowCancellation"] = "requested"
            return "requested"
        if attempt < 2:
            time.sleep(2)
    evidence["workflowCancellation"] = "failed"
    return "failed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("recovery", "ordinary"), default="recovery")
    parser.add_argument("--url", required=True)
    parser.add_argument("--recovery-id", default="")
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
    if args.mode == "recovery" and not args.recovery_id:
        raise ValueError("recovery-id is required in recovery mode")
    if args.mode == "ordinary" and args.attempt < 1:
        raise ValueError("ordinary pipeline attempt must be positive")
    deadline = datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    headers = _headers()
    evidence: dict[str, Any] = {
        "schemaVersion": 1,
        "mode": args.mode,
        "status": "RUNNING",
        "runKey": args.run_key,
        "leaseOwner": args.lease_owner,
        "attemptCount": args.attempt,
        "renewalRequestCount": 0,
        "renewalCount": 0,
        "unknownRenewalCount": 0,
        "rowsWritten": 0,
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
                evidence["errorCode"] = (
                    "live_recovery_deadline_expired"
                    if args.mode == "recovery"
                    else "pipeline_window_lease_heartbeat_deadline_expired"
                )
                _write(args.evidence, evidence)
                if args.mode == "ordinary":
                    _cancel_ordinary_workflow(evidence)
                    _write(args.evidence, evidence)
                    raise SystemExit(2)
                return
            until_renewal = next_renewal - time.monotonic()
            if until_renewal > 0:
                # Local stop polling is not a network renewal.
                remaining = (deadline - now).total_seconds()
                time.sleep(min(until_renewal, remaining, 5.0))
                continue
            payload: dict[str, Any] = {
                "runKey": args.run_key,
                "leaseOwner": args.lease_owner,
                "expectedAttemptCount": args.attempt,
                "status": "RUNNING",
                "renewLease": True,
            }
            if args.mode == "recovery":
                payload["recoveryId"] = args.recovery_id
            evidence["renewalRequestCount"] += 1
            try:
                status_code, body = _post_json(args.url, payload, headers, timeout=30)
            except Exception:
                evidence["unknownRenewalCount"] += 1
                evidence["lastRenewalOutcome"] = "unknown"
                _write(args.evidence, evidence)
                raise
            if status_code != 200:
                if isinstance(body, dict) and isinstance(body.get("httpDiagnostic"), dict):
                    evidence["httpDiagnostic"] = body["httpDiagnostic"]
                evidence["unknownRenewalCount"] += 1
                evidence["lastRenewalOutcome"] = "unknown"
                evidence.update({
                    "status": "FAILED",
                    "errorCode": (
                        "live_recovery_lease_renew_failed"
                        if args.mode == "recovery"
                        else "pipeline_window_lease_renew_failed"
                    ),
                    "httpStatus": status_code,
                })
                _write(args.evidence, evidence)
                raise RuntimeError("lease renewal was rejected")
            try:
                validated = _validate_renewal(
                    args.mode, body, run_key=args.run_key, lease_owner=args.lease_owner,
                    attempt=args.attempt, now=now,
                )
            except Exception:
                evidence["unknownRenewalCount"] += 1
                evidence["lastRenewalOutcome"] = "unknown"
                _write(args.evidence, evidence)
                raise
            evidence["renewalCount"] += 1
            evidence["lastRenewalOutcome"] = "confirmed"
            evidence["leaseExpiresAt"] = validated["leaseExpiresAt"]
            if args.mode == "recovery":
                evidence["recoveryStatus"] = validated["recoveryStatus"]
                for key in ("rowsWritten", "rowsRead"):
                    if key in validated:
                        evidence[key] = validated[key]
            else:
                evidence["rowsWritten"] += validated["rowsWritten"]
            evidence["measuredAt"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
            _write(args.evidence, evidence)
            next_renewal = time.monotonic() + args.interval_seconds
    except SystemExit:
        raise
    except Exception as exc:
        evidence["status"] = "FAILED"
        if isinstance(exc, ValueError) and str(exc).startswith(("live_recovery_", "pipeline_window_")):
            evidence["errorCode"] = str(exc)
        elif not evidence.get("errorCode"):
            evidence["errorCode"] = (
                "live_recovery_lease_renew_failed"
                if args.mode == "recovery"
                else "pipeline_window_lease_renew_failed"
            )
        evidence["detail"] = type(exc).__name__
        if args.mode == "ordinary":
            _cancel_ordinary_workflow(evidence)
        _write(args.evidence, evidence)
        print(json.dumps({
            "status": evidence["status"],
            "errorCode": evidence["errorCode"],
            "workflowCancellation": evidence.get("workflowCancellation", "not_applicable"),
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        _write(args.evidence, evidence)


if __name__ == "__main__":
    main()
