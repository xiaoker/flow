"""Bounded ordinary-run lease verification and cooperative renewal.

No claim, dispatch, completion, date binding or recovery operation is exposed.
Call check at every side-effect boundary. Long local computations do not spawn
an unbounded heartbeat process: an expired lease prevents their next effect or
completion. Production entry points remain unchanged.
"""

from __future__ import annotations

import json
import math
import time
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from .http import HttpSettings, build_session
from .pipeline_runner import PipelineExecutionContext, PipelineRunnerError


class OrdinaryPipelineLease:
    """Use the existing authenticated status/renew API with attempt fencing."""

    def __init__(
        self,
        *,
        base_url: str,
        ingest_key: str,
        access_client_id: str,
        access_client_secret: str,
        expected_attempt_count: int,
        maximum_requests: int = 32,
        maximum_seconds: float = 3600,
        renewal_interval_seconds: float = 180,
        session: Any = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        allow_loopback_fixture: bool = False,
    ) -> None:
        parsed = urlparse(base_url)
        fixture = allow_loopback_fixture and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if (
            not ((parsed.scheme == "https" and parsed.hostname == "flow.xiaoker.com" and parsed.port in {None, 443}) or fixture)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise PipelineRunnerError("pipeline_lease_origin_invalid")
        if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value
               for value in (ingest_key, access_client_id, access_client_secret)):
            raise PipelineRunnerError("pipeline_lease_credentials_required")
        if isinstance(expected_attempt_count, bool) or not isinstance(expected_attempt_count, int) or expected_attempt_count < 1:
            raise PipelineRunnerError("pipeline_lease_attempt_invalid")
        if isinstance(maximum_requests, bool) or not isinstance(maximum_requests, int) or not 1 <= maximum_requests <= 32:
            raise PipelineRunnerError("pipeline_lease_request_budget_invalid")
        for value, low, high in ((maximum_seconds, 1, 3600), (renewal_interval_seconds, 60, 300)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise PipelineRunnerError("pipeline_lease_time_budget_invalid")
        self._url = base_url.rstrip("/") + "/api/v1/admin/pipeline-window/status"
        self._headers = {"Authorization": f"Bearer {ingest_key}", "CF-Access-Client-Id": access_client_id,
                         "CF-Access-Client-Secret": access_client_secret, "Content-Type": "application/json"}
        self._session = session if session is not None else build_session(HttpSettings(total_retries=0))
        adapters = getattr(self._session, "adapters", {})
        if any(getattr(getattr(adapter, "max_retries", None), "total", None) not in {0, False}
               for adapter in adapters.values()):
            raise PipelineRunnerError("pipeline_lease_hidden_retries_forbidden")
        self._attempt = expected_attempt_count
        self._maximum_requests = maximum_requests
        self._maximum_seconds = float(maximum_seconds)
        self._interval = float(renewal_interval_seconds)
        self._now = now
        self._monotonic = monotonic
        self._context: PipelineExecutionContext | None = None
        self._started = 0.0
        self._expires = 0.0
        self._next_renewal = 0.0
        self._failed: str | None = None
        self.request_count = 0
        self.renewal_count = 0
        self.renewal_attempt_count = 0
        self.rows_written = 0
        self._verified_expiry: str | None = None
        self._lock = threading.Lock()

    def _fail(self, code: str) -> None:
        self._failed = code
        raise PipelineRunnerError(code) from None

    def verify(self, context: PipelineExecutionContext) -> bool:
        with self._lock:
            return self._verify(context)

    def _verify(self, context: PipelineExecutionContext) -> bool:
        if self._context is not None:
            self._fail("pipeline_lease_guard_already_used")
        self._validate_context(context)
        if context.lease_attempt_count != self._attempt:
            self._fail("pipeline_lease_attempt_mismatch")
        self._context = context
        self._started = self._monotonic()
        self._request(renew=False)
        return True

    def _validate_context(self, context: PipelineExecutionContext) -> None:
        if context.mode != "ordinary" or context.recovery_id is not None:
            self._fail("pipeline_lease_recovery_requires_existing_recovery_guard")

    def check(self) -> None:
        with self._lock:
            self._check()

    def _check(self) -> None:
        if self._failed:
            raise PipelineRunnerError(self._failed)
        if self._context is None:
            self._fail("pipeline_lease_not_verified")
        current = self._monotonic()
        if current - self._started >= self._maximum_seconds:
            self._fail("pipeline_lease_execution_deadline")
        # Never send a renewal that attempts to revive an already expired owner.
        if current >= self._expires:
            self._fail("pipeline_lease_expired")
        if current >= self._next_renewal:
            self._request(renew=True)

    def confirm(self) -> None:
        """Fresh authority proof before persisting a stage/result receipt."""
        with self._lock:
            before = self.request_count
            self._check()
            if self.request_count == before:
                self._request(renew=False)

    def _request(self, *, renew: bool) -> None:
        if self.request_count >= self._maximum_requests:
            self._fail("pipeline_lease_request_budget_exceeded")
        context = self._context
        assert context is not None
        self.request_count += 1
        if renew:
            self.renewal_attempt_count += 1
        kwargs: dict[str, Any] = {"headers": self._headers, "timeout": (5, 10), "allow_redirects": False, "stream": True}
        if renew:
            kwargs["json"] = {"runKey": context.identity.run_key, "leaseOwner": context.lease_owner,
                              "expectedAttemptCount": self._attempt, "status": "RUNNING", "renewLease": True}
        else:
            kwargs["params"] = {"runKey": context.identity.run_key}
        response = None
        request_started = self._monotonic()
        try:
            response = self._session.request("POST" if renew else "GET", self._url, **kwargs)
            if response.status_code != 200:
                self._fail("pipeline_lease_http_rejected")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                if self._monotonic() - request_started >= 20:
                    self._fail("pipeline_lease_response_deadline")
                content.extend(chunk)
                if len(content) > 64000:
                    self._fail("pipeline_lease_response_too_large")
            body = json.loads(content)
            self._validate(body, renew=renew)
        except PipelineRunnerError:
            raise
        except Exception:
            # Never echo provider bodies, URLs, headers, or exception strings.
            self._fail("pipeline_lease_transport_or_payload_failed")
        finally:
            if response is not None:
                response.close()

    def _validate(self, body: Any, *, renew: bool) -> None:
        context = self._context
        assert context is not None
        if not isinstance(body, Mapping):
            self._fail("pipeline_lease_payload_invalid")
        if not {"runKey", "leaseOwner", "attemptCount", "status", "recoveryId", "windowDate", "targetDate", "leaseExpiresAt"}.issubset(body):
            self._fail("pipeline_lease_payload_invalid")
        if (body.get("runKey") != context.identity.run_key or body.get("leaseOwner") != context.lease_owner
            or isinstance(body.get("attemptCount"), bool) or not isinstance(body.get("attemptCount"), int)
            or body.get("attemptCount") != self._attempt
            or body.get("status") not in {"DISPATCHED", "RUNNING"} or body.get("recoveryId") is not None
            or body.get("windowDate") != context.identity.window_date
            or body.get("targetDate") != context.identity.target_date):
            self._fail("pipeline_lease_identity_conflict")
        try:
            expiry = datetime.fromisoformat(body["leaseExpiresAt"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("timezone_required")
            remaining = (expiry - self._now()).total_seconds()
        except (ValueError, TypeError, KeyError, AttributeError):
            self._fail("pipeline_lease_expiry_invalid")
        current = self._monotonic()
        if remaining < 30 or current - self._started >= self._maximum_seconds:
            self._fail("pipeline_lease_expired")
        if renew:
            written = body.get("rowsWritten")
            if body.get("decision") != "renewed" or isinstance(written, bool) or not isinstance(written, int) or not 1 <= written <= 32:
                self._fail("pipeline_lease_renewal_receipt_invalid")
            self.renewal_count += 1
            self.rows_written += written
        was_verified = self._verified_expiry is not None
        self._expires = current + remaining
        self._verified_expiry = expiry.isoformat()
        next_renewal = current + min(self._interval, remaining / 2)
        self._next_renewal = min(self._next_renewal, next_renewal) if was_verified and not renew else next_renewal

    def evidence(self) -> dict[str, Any]:
        return {"requestCount": self.request_count, "requestBudget": self._maximum_requests,
                "renewalCount": self.renewal_count, "rowsWritten": self.rows_written,
                "renewalAttemptCount": self.renewal_attempt_count,
                "unconfirmedWriteOutcome": self.renewal_attempt_count != self.renewal_count,
                "verifiedLeaseExpiresAt": self._verified_expiry,
                "attemptCount": self._attempt,
                "rowsRead": {"value": None, "basis": "unknown"},
                "accountRemainingBudget": {"value": None, "basis": "unknown"},
                "maximumSeconds": self._maximum_seconds, "errorCode": self._failed,
                "policy": "cooperative_boundary_checks_no_claim_or_completion"}
