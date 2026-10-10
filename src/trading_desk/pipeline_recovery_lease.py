"""Candidate recovery authority guard using existing approval and lease APIs.

No registration, claim, completion, plan download, budget grant or activation.
The shared lifecycle is inherited; ordinary authority checks remain unchanged.
"""
import copy
import json
from datetime import datetime
from typing import Mapping

from .bounded_http import BoundedHTTPExchange
from .pipeline_lease import OrdinaryPipelineLease
from .pipeline_recovery_scope import bind_recovery_runner_scope
from .pipeline_runner import PipelineRunnerError
from .recovery_execution import plan_bytes, validate_recovery_plan


class RecoveryPipelineLease(OrdinaryPipelineLease):
    def __init__(self, *, approved_plan, stages, **kwargs):
        self._plan = copy.deepcopy(approved_plan)
        self._scope = validate_recovery_plan(self._plan)
        self._scope_sha = plan_bytes(self._plan)[1]
        self._stages = tuple(stages)
        super().__init__(**kwargs)
        self._maximum_requests = min(self._maximum_requests, self._scope.max_requests)
        self._origin = self._url.removesuffix("/api/v1/admin/pipeline-window/status")
        self._exchange = BoundedHTTPExchange(self._session, monotonic=self._monotonic)

    def _validate_context(self, context):
        try:
            bind_recovery_runner_scope(self._plan, context, self._stages, now=self._now())
        except ValueError:
            self._fail("recovery_lease_scope_rejected")

    def _expiry(self, value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            return parsed
        except (ValueError, TypeError, AttributeError):
            self._fail("recovery_lease_expiry_invalid")

    def _fetch(self, path, *, payload=None, params=None):
        if self.request_count >= self._maximum_requests:
            self._fail("pipeline_lease_request_budget_exceeded")
        if self._monotonic() - self._started >= self._maximum_seconds:
            self._fail("pipeline_lease_execution_deadline")
        self.request_count += 1
        try:
            reply = self._exchange.request("POST" if payload else "GET", self._origin + path,
                headers=self._headers, **({"json": payload} if payload else {"params": params}),
                max_bytes=64000, chunk_size=4096, max_chunks=64, deadline_seconds=20)
            if reply.status != 200:
                self._fail("pipeline_lease_http_rejected")
            body = json.loads(reply.content)
            if not isinstance(body, Mapping):
                self._fail("pipeline_lease_payload_invalid")
            return body
        except PipelineRunnerError:
            raise
        except Exception:
            self._fail("pipeline_lease_transport_or_payload_failed")

    def _identity(self, body):
        context = self._context
        if (body.get("runKey") != context.identity.run_key
                or body.get("leaseOwner") != context.lease_owner
                or type(body.get("attemptCount")) is not int
                or body.get("attemptCount") != self._attempt
                or body.get("status") not in {"DISPATCHED", "RUNNING"}):
            self._fail("pipeline_lease_identity_conflict")

    def _request(self, *, renew):
        context = self._context
        assert context is not None
        self._validate_context(context)
        if renew:
            # Reserve all three requests before a potentially successful write.
            if self.request_count + 3 > self._maximum_requests:
                self._fail("pipeline_lease_request_budget_exceeded")
            self.renewal_attempt_count += 1
            receipt = self._fetch("/api/v1/admin/pipeline-window/recovery/status", payload={
                "recoveryId": context.recovery_id, "runKey": context.identity.run_key,
                "leaseOwner": context.lease_owner, "expectedAttemptCount": self._attempt,
                "status": "RUNNING", "renewLease": True})
            self._identity(receipt)
            written = receipt.get("rowsWritten")
            if (receipt.get("recoveryId") != context.recovery_id or receipt.get("recoveryStatus") != "ACTIVE"
                    or type(written) is not int or not 1 <= written <= 32):
                self._fail("pipeline_lease_renewal_receipt_invalid")
            self.renewal_count += 1
            self.rows_written += written
        elif self.request_count + 2 > self._maximum_requests:
            self._fail("pipeline_lease_request_budget_exceeded")
        # The compact renewal response cannot prove approval scope. Always read
        # both existing endpoints: recovery approval and current execution row.
        approval = self._fetch("/api/v1/admin/pipeline-window/recovery", params={"recoveryId": context.recovery_id})
        if (approval.get("recoveryId") != context.recovery_id
                or approval.get("runKey") != context.identity.run_key
                or approval.get("planSha256") != self._scope.plan_hash
                or approval.get("scopeSha256") != self._scope_sha
                or approval.get("planKey") != f"market-data/recovery/plans/{context.recovery_id}-{self._scope_sha}.json"
                or approval.get("status") != "ACTIVE"):
            self._fail("recovery_lease_approval_conflict")
        pipeline = approval.get("pipeline")
        if not isinstance(pipeline, Mapping):
            self._fail("pipeline_lease_payload_invalid")
        self._identity(pipeline)
        current = self._fetch("/api/v1/admin/pipeline-window/status", params={"runKey": context.identity.run_key})
        self._identity(current)
        if (current.get("recoveryId") != context.recovery_id
                or current.get("windowDate") != context.identity.window_date
                or current.get("targetDate") != context.identity.target_date):
            self._fail("pipeline_lease_identity_conflict")
        expiry = min(self._expiry(approval.get("expiresAt")), self._expiry(self._scope.deadline),
            self._expiry(pipeline.get("leaseExpiresAt")), self._expiry(current.get("leaseExpiresAt")))
        remaining = (expiry - self._now()).total_seconds()
        clock = self._monotonic()
        if remaining < 30 or clock - self._started >= self._maximum_seconds:
            self._fail("pipeline_lease_expired")
        next_renewal = clock + min(self._interval, remaining / 2)
        self._next_renewal = (min(self._next_renewal, next_renewal)
            if self._verified_expiry is not None and not renew else next_renewal)
        self._expires, self._verified_expiry = clock + remaining, expiry.isoformat()

    def evidence(self):
        return {**super().evidence(), "recoveryId": self._scope.recovery_id,
            "planSha256": self._scope.plan_hash, "scopeSha256": self._scope_sha,
            "policy": "existing_recovery_approval_and_lease_no_claim_completion_or_budget_grant"}
