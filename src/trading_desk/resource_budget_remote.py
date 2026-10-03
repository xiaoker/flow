"""Bounded candidate adapter for the server-authoritative shared ledger.

No automatic retries, account initialization, evidence refresh or period reset.
Lost permission replies retain holds and prohibit I/O. Explicit reconciliation
may recover bookkeeping, but a replayed allowance never grants another effect.
Control-plane requests themselves need a separately proven task upper bound;
this adapter does not claim whole-account telemetry or enable a live entry.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from .http import HttpSettings, build_session
from .resource_budget import BudgetLedger, BudgetVector

MAX_REQUEST_SECONDS = 20

class SharedBudgetLedger(BudgetLedger):
    """Use existing admin auth and fenced ordinary pipeline identity only."""

    def __init__(self, *, base_url: str, ingest_key: str, access_client_id: str,
                 access_client_secret: str, run_key: str, lease_owner: str,
                 attempt_count: int, target_date: str, lease_guard: Any,
                 maximum_requests: int = 32, session: Any = None,
                 allow_loopback_fixture: bool = False, monotonic=time.monotonic):
        from .pipeline_application import parse_application_run_key
        parsed = urlparse(base_url)
        fixture = allow_loopback_fixture and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if (not ((parsed.scheme == "https" and parsed.hostname == "flow.xiaoker.com" and parsed.port in {None, 443}) or fixture)
                or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("shared_budget_origin_invalid")
        import re
        from datetime import date
        parse_application_run_key(run_key)
        if not isinstance(lease_owner, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", lease_owner):
            raise ValueError("shared_budget_owner_invalid")
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or not 1 <= attempt_count <= 999999999:
            raise ValueError("shared_budget_attempt_invalid")
        if not isinstance(target_date, str) or date.fromisoformat(target_date).isoformat() != target_date:
            raise ValueError("shared_budget_date_invalid")
        if isinstance(maximum_requests, bool) or not isinstance(maximum_requests, int) or not 1 <= maximum_requests <= 32:
            raise ValueError("shared_budget_request_limit_invalid")
        if not callable(getattr(lease_guard, "confirm", None)):
            raise ValueError("shared_budget_lease_guard_required")
        if any(not isinstance(v, str) or not v.strip() or "\r" in v or "\n" in v
               for v in (ingest_key, access_client_id, access_client_secret)):
            raise ValueError("shared_budget_credentials_required")
        self._session = session if session is not None else build_session(HttpSettings(total_retries=0))
        if any(getattr(getattr(a, "max_retries", None), "total", None) not in {0, False}
               for a in getattr(self._session, "adapters", {}).values()):
            raise ValueError("shared_budget_hidden_retries_forbidden")
        self._url = base_url.rstrip("/") + "/api/v1/admin/pipeline-window/budget"
        self._headers = {"Authorization": f"Bearer {ingest_key}", "CF-Access-Client-Id": access_client_id,
                         "CF-Access-Client-Secret": access_client_secret, "Content-Type": "application/json"}
        self._identity = {"runKey": run_key, "leaseOwner": lease_owner, "attemptCount": attempt_count, "targetDate": target_date}
        self.task_id = f"{run_key}/{lease_owner}/{attempt_count}"
        self._guard, self._clock, self._maximum = lease_guard, monotonic, maximum_requests
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self.request_count = 0
        self.rows_read: int | None = 0
        self.rows_written: int | None = 0
        self._inflight: threading.Thread | None = None

    def _request(self, method: str, payload: dict) -> dict:
        if self.request_count >= self._maximum:
            raise ValueError("shared_budget_request_limit")
        if self._inflight is not None and self._inflight.is_alive():
            raise ValueError("shared_budget_request_still_unconfirmed")
        self._guard.confirm()
        self.request_count += 1
        received_meta = False
        exchange: dict = {}
        # A read timeout alone does not bound a slow-drip body/header. Only the
        # foreground may grant permission; a timed-out daemon can at most finish
        # this same budget operation. No new request is allowed while it lives.
        def perform():
            response = None
            started = self._clock()
            try:
                response = self._session.request(method, self._url, params=payload if method == "GET" else None,
                    data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode() if method == "POST" else None,
                    headers=self._headers, timeout=(5, 10), stream=True, allow_redirects=False)
                content = bytearray()
                for count, chunk in enumerate(response.iter_content(chunk_size=4096)):
                    content.extend(chunk)
                    if len(content) > 16384 or count >= 64 or self._clock() - started >= MAX_REQUEST_SECONDS:
                        raise ValueError("shared_budget_response_limit")
                exchange.update(status=response.status_code, content=bytes(content))
            except Exception:
                exchange["error"] = True
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        exchange["error"] = True
        try:
            self._inflight = threading.Thread(target=perform, daemon=True)
            self._inflight.start()
            self._inflight.join(timeout=MAX_REQUEST_SECONDS)
            if self._inflight.is_alive() or exchange.get("error"):
                raise ValueError("shared_budget_response_limit")
            result = json.loads(exchange["content"])
            if not isinstance(result, dict):
                raise ValueError("shared_budget_invalid_response")
            for name, field in (("rows_read", "rowsRead"), ("rows_written", "rowsWritten")):
                value = result.get(field)
                previous = getattr(self, name)
                setattr(self, name, previous + value if previous is not None and isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None)
            received_meta = True
            if exchange["status"] != 200:
                # 409 is a definitive rejection, not a successful hidden commit.
                if exchange["status"] in {401, 409, 413, 422}:
                    self._pending = None
                raise ValueError("shared_budget_request_rejected")
            self._guard.confirm()
            return result
        except Exception:
            if not received_meta:
                self.rows_read = self.rows_written = None
            raise ValueError("shared_budget_request_failed; allowance_not_granted") from None

    def snapshot(self) -> dict:
        with self._lock:
            result = self._request("GET", self._identity)
            revision = result.get("revision")
            if result.get("contract") != "flow-shared-budget-v1" or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError("shared_budget_contract_invalid")
            return result  # Metadata, not a full account-availability claim.

    def _execute(self, task_id: str, operation: str, **kwargs: Any) -> dict:
        with self._lock:
            if task_id != self.task_id:
                raise ValueError("shared_budget_task_mismatch")
            if self._pending is not None:
                raise ValueError("shared_budget_outcome_unknown; reconcile_before_new_operation")
            revision = self.snapshot()["revision"]
            operation_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            self._pending = {**self._identity, "operationId": operation_id, "operation": operation,
                             "expectedRevision": revision, **kwargs}
            result = self._request("POST", self._pending)
            self._validate_result(result, operation)
            self._pending = None
            return result

    def _validate_result(self, result: dict, operation: str) -> None:
        allowed = {"reserve": {"allow", "warn", "idempotent", "unknown_metrics", "over_budget"},
                   "mark_running": {"running"}, "authorize_step": {"authorized", "not_authorized", "unknown_metrics", "step_over_budget"},
                   "debit": {"debited", "not_debitable", "over_budget"},
                   "settle": {"settled", "unknown_outcome", "over_budget"}}
        revision = result.get("revision")
        if (result.get("decision") not in allowed[operation] or not isinstance(result.get("replayed"), bool)
                or result.get("taskId") != self.task_id or isinstance(revision, bool)
                or not isinstance(revision, int) or revision < 1):
            raise ValueError("shared_budget_invalid_receipt")
        if operation == "authorize_step" and result["replayed"]:
            raise ValueError("shared_budget_replayed_allowance_not_executable")

    def resolve_pending(self) -> dict:
        """One explicit same-ID attempt; never creates a new allowance ID."""
        with self._lock:
            if self._pending is None:
                raise ValueError("shared_budget_no_pending_operation")
            operation = self._pending["operation"]
            result = self._request("POST", self._pending)
            # Even a freshly committed late authorization is not returned as
            # execution permission. The caller stopped on an uncertain result.
            if operation == "authorize_step":
                # Validate the receipt before clearing ambiguity; temporarily
                # strip replay solely for validation, never for execution.
                self._validate_result({**result, "replayed": False}, operation)
                self._pending = None
                return {**result, "decision": "not_authorized", "reason": "ambiguous_allowance_retained"}
            self._validate_result(result, operation)
            self._pending = None
            return result

    def reserve(self, task_id: str, task_cost: BudgetVector) -> dict:
        return self._execute(task_id, "reserve", vector=task_cost.as_dict())

    def mark_running(self, task_id: str) -> dict:
        return self._execute(task_id, "mark_running")

    def authorize_step(self, task_id: str, cost_upper_bound: BudgetVector) -> dict:
        return self._execute(task_id, "authorize_step", vector=cost_upper_bound.as_dict())

    def debit(self, task_id: str, delta: BudgetVector) -> dict:
        return self._execute(task_id, "debit", vector=delta.as_dict())

    def settle(self, task_id: str, *, outcome: str, actual: BudgetVector | None = None, reason: str | None = None) -> dict:
        return self._execute(task_id, "settle", outcome=outcome, **({"vector": actual.as_dict()} if actual else {}))

    def expire(self, **kwargs: Any):
        raise ValueError("shared_budget_expiry_is_server_authoritative")
