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
from .bounded_http import BoundedHTTPExchange
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
        self._exchange = BoundedHTTPExchange(self._session, monotonic=monotonic)

    @property
    def _inflight(self):
        return self._exchange.inflight

    def validate_execution_identity(self, *, run_key: str, lease_owner: str,
                                    attempt_count: int, target_date: str,
                                    task_id: str) -> None:
        """Local equality fence, not lease verification or budget permission."""
        if (task_id != self.task_id or type(attempt_count) is not int
                or self._identity != {"runKey": run_key, "leaseOwner": lease_owner,
                                      "attemptCount": attempt_count, "targetDate": target_date}):
            raise ValueError("shared_budget_execution_identity_mismatch")

    def accounting_evidence(self) -> dict:
        """Already observed control-plane usage only; never performs a GET."""
        with self._lock:
            return {"scope": "server_authoritative_shared", "requests": self.request_count,
                    "maximumRequests": self._maximum, "d1RowsRead": self.rows_read,
                    "d1RowsWritten": self.rows_written,
                    "pendingOperation": self._pending is not None,
                    "accountRemaining": None}

    def _request(self, method: str, payload: dict) -> dict:
        if self.request_count >= self._maximum:
            raise ValueError("shared_budget_request_limit")
        if self._inflight is not None and self._inflight.is_alive():
            raise ValueError("shared_budget_request_still_unconfirmed")
        self._guard.confirm()
        self.request_count += 1
        received_meta = False
        try:
            reply = self._exchange.request(method, self._url, max_bytes=16384,
                chunk_size=4096, max_chunks=64, deadline_seconds=MAX_REQUEST_SECONDS,
                params=payload if method == "GET" else None,
                data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode() if method == "POST" else None,
                headers=self._headers)
            result = json.loads(reply.content)
            if not isinstance(result, dict):
                raise ValueError("shared_budget_invalid_response")
            for name, field in (("rows_read", "rowsRead"), ("rows_written", "rowsWritten")):
                value = result.get(field)
                previous = getattr(self, name)
                setattr(self, name, previous + value if previous is not None and isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None)
            received_meta = True
            if reply.status != 200:
                # 409 is a definitive rejection, not a successful hidden commit.
                if reply.status in {401, 409, 413, 422}:
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


class RecoverySharedBudgetLedger(SharedBudgetLedger):
    """Same account ledger; exact approved recovery scope, never a new quota."""

    def __init__(self, *, approved_plan, context, stages, now=None, **kwargs):
        from .pipeline_recovery_scope import bind_recovery_runner_scope
        from .recovery_execution import plan_bytes, validate_recovery_plan
        bind_recovery_runner_scope(approved_plan, context, stages, now=now)
        scope = validate_recovery_plan(approved_plan)
        manifest = scope.budget_manifest
        if not isinstance(manifest, dict) or manifest.get("runnerBudgetContract") != "flow-shared-budget-v1":
            raise ValueError("budget_recovery_upper_bound_required")
        upper = manifest.get("taskCostUpperBound")
        if not isinstance(upper, dict) or len(upper) != 7:
            raise ValueError("budget_recovery_upper_bound_required")
        self._approved_upper = BudgetVector.from_mapping(upper)
        if not self._approved_upper.all_known():
            raise ValueError("budget_recovery_upper_bound_unknown")
        super().__init__(**kwargs)
        self.validate_execution_identity(run_key=context.identity.run_key, lease_owner=context.lease_owner,
            attempt_count=context.lease_attempt_count, target_date=context.identity.target_date, task_id=self.task_id)
        self._recovery_binding = {"recoveryId": scope.recovery_id, "planSha256": scope.plan_hash,
                                  "scopeSha256": plan_bytes(approved_plan)[1]}
        self._identity.update(self._recovery_binding)
        self.task_id = f"recovery/{scope.recovery_id}/{scope.plan_hash}/{self.task_id}"
        self._maximum = min(self._maximum, scope.max_requests)

    def validate_execution_identity(self, *, run_key, lease_owner, attempt_count,
                                    target_date, task_id, recovery_id=None, recovery_plan_sha256=None):
        if not hasattr(self, "_recovery_binding"):
            return super().validate_execution_identity(run_key=run_key, lease_owner=lease_owner,
                attempt_count=attempt_count, target_date=target_date, task_id=task_id)
        if (recovery_id != self._recovery_binding["recoveryId"]
                or recovery_plan_sha256 != self._recovery_binding["planSha256"]
                or task_id != self.task_id or type(attempt_count) is not int
                or self._identity != {"runKey": run_key, "leaseOwner": lease_owner,
                    "attemptCount": attempt_count, "targetDate": target_date, **self._recovery_binding}):
            raise ValueError("shared_budget_execution_identity_mismatch")

    def reserve(self, task_id, task_cost):
        if (not task_cost.all_known() or any(task_cost.metrics[d].value > self._approved_upper.metrics[d].value
                                           for d in self._approved_upper.metrics)):
            raise ValueError("budget_recovery_scope_over_budget")
        return super().reserve(task_id, task_cost)

    def accounting_evidence(self):
        return {**super().accounting_evidence(), **self._recovery_binding}
