"""Candidate bounded immutable R2 transport via existing Flow admin authority.

No bucket listing, claim, completion, date binding or active release operation.
The caller supplies an already verified ordinary lease and trusted object hash.
"""
from __future__ import annotations

import hashlib
import json
import time
import threading
from typing import Any
from urllib.parse import urlparse

from .http import HttpSettings, build_session
from .pipeline_checkpoint_bundle import verify_stage_bundle
from .pipeline_runner import PipelineExecutionContext, PipelineRunnerError
from .resource_budget import BudgetLedger, BudgetVector

MAX_OBJECT_BYTES = 1024 * 1024


class CheckpointObjectTransport:
    def __init__(self, *, base_url: str, ingest_key: str, access_client_id: str,
                 access_client_secret: str, lease_guard: Any, maximum_requests: int = 12,
                 session: Any = None, monotonic=time.monotonic, allow_loopback_fixture: bool = False,
                 budget_ledger: BudgetLedger | None = None, budget_task_id: str | None = None,
                 request_costs: dict[str, BudgetVector] | None = None):
        parsed = urlparse(base_url)
        fixture = allow_loopback_fixture and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if not ((parsed.scheme == "https" and parsed.hostname == "flow.xiaoker.com" and parsed.port in {None, 443}) or fixture) or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise PipelineRunnerError("checkpoint_transport_origin_invalid")
        if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value
               for value in (ingest_key, access_client_id, access_client_secret)):
            raise PipelineRunnerError("checkpoint_transport_credentials_required")
        if isinstance(maximum_requests, bool) or not isinstance(maximum_requests, int) or not 1 <= maximum_requests <= 12:
            raise PipelineRunnerError("checkpoint_transport_budget_invalid")
        self._session = session if session is not None else build_session(HttpSettings(total_retries=0))
        if any(getattr(getattr(adapter, "max_retries", None), "total", None) not in {0, False}
               for adapter in getattr(self._session, "adapters", {}).values()):
            raise PipelineRunnerError("checkpoint_transport_hidden_retries_forbidden")
        self._url = base_url.rstrip("/") + "/api/v1/admin/pipeline-window/checkpoint"
        self._headers = {"Authorization": f"Bearer {ingest_key}", "CF-Access-Client-Id": access_client_id,
                         "CF-Access-Client-Secret": access_client_secret, "Content-Type": "application/json"}
        self._guard, self._maximum, self._clock = lease_guard, maximum_requests, monotonic
        self.request_count = 0
        self.upload_attempts = 0
        self.downloads = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.unconfirmed_write_outcome = False
        self._lock = threading.RLock()
        self._budget_ledger, self._budget_task_id = budget_ledger, budget_task_id
        self._request_costs = dict(request_costs or {})

    def _authorize_request(self, method: str, data: bytes | None) -> None:
        cost = self._request_costs.get(method)
        if not isinstance(self._budget_ledger, BudgetLedger) or not self._budget_task_id or not isinstance(cost, BudgetVector) or not cost.all_known():
            raise PipelineRunnerError("checkpoint_resource_allowance_required")
        # Includes the server's readback and caller's lease checks/renewals.
        # These floors do not manufacture a complete estimate; caller supplies
        # all seven dimensions, including total lease and execution upper bounds.
        floors = {"workerRequests": 1, "r2ClassB": 1, "d1RowsRead": 1}
        if method == "POST":
            floors.update(r2ClassA=1, r2Bytes=len(data or b""))
        if any(cost.metrics[name].value < minimum for name, minimum in floors.items()):
            raise PipelineRunnerError("checkpoint_resource_cost_understated")
        try:
            result = self._budget_ledger.authorize_step(self._budget_task_id, cost)
        except ValueError:
            raise PipelineRunnerError("checkpoint_resource_allowance_rejected") from None
        if result["decision"] != "authorized":
            raise PipelineRunnerError("checkpoint_resource_allowance_rejected")

    @staticmethod
    def _params(context: PipelineExecutionContext, stage: str, input_sha256: str, sha256: str) -> dict:
        if context.mode != "ordinary" or context.recovery_id is not None or context.lease_attempt_count is None or context.identity.target_date is None:
            raise PipelineRunnerError("checkpoint_transport_requires_bound_ordinary_lease")
        return {"runKey": context.identity.run_key, "targetDate": context.identity.target_date,
                "stage": stage, "inputSha256": input_sha256, "sha256": sha256,
                "producerBuildSha": context.identity.producer_build_sha,
                "leaseOwner": context.lease_owner, "attemptCount": context.lease_attempt_count}

    @staticmethod
    def key(context: PipelineExecutionContext, stage: str, input_sha256: str, sha256: str) -> str:
        run_hash = hashlib.sha256(context.identity.run_key.encode()).hexdigest()
        return f"market-data/pipeline/checkpoints/v1/{run_hash}/{context.identity.producer_build_sha}/{stage}/{input_sha256}/{sha256}.json"

    def _request(self, method: str, params: dict, data: bytes | None = None) -> tuple[int, bytes, Any]:
        if self.request_count >= self._maximum:
            raise PipelineRunnerError("checkpoint_transport_budget_exceeded")
        self._authorize_request(method, data)
        self._guard.confirm()
        self.request_count += 1
        if method == "POST":
            self.upload_attempts += 1
            self.bytes_sent += len(data or b"")
            self.unconfirmed_write_outcome = True
        response = None
        started = self._clock()
        try:
            response = self._session.request(method, self._url, params=params, data=data, headers=self._headers,
                                             allow_redirects=False, timeout=(5, 10), stream=True)
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                self.bytes_received += len(chunk)
                content.extend(chunk)
                if len(content) > MAX_OBJECT_BYTES or self._clock() - started >= 20:
                    raise PipelineRunnerError("checkpoint_transport_response_limit")
            if self._clock() - started >= 20:
                raise PipelineRunnerError("checkpoint_transport_response_limit")
            return response.status_code, bytes(content), response.headers
        except PipelineRunnerError:
            raise
        except Exception:
            raise PipelineRunnerError("checkpoint_transport_unavailable") from None
        finally:
            if response is not None:
                response.close()

    def download(self, *, context: PipelineExecutionContext, stage: str, input_sha256: str, sha256: str) -> bytes:
        with self._lock:
            return self._download(context=context, stage=stage, input_sha256=input_sha256, sha256=sha256)

    def _download(self, *, context: PipelineExecutionContext, stage: str, input_sha256: str, sha256: str) -> bytes:
        params = self._params(context, stage, input_sha256, sha256)
        status, content, headers = self._request("GET", params)
        if status != 200 or headers.get("X-Flow-Checkpoint-Sha256") != sha256 or headers.get("X-Flow-Checkpoint-Key") != self.key(context, stage, input_sha256, sha256):
            raise PipelineRunnerError("checkpoint_transport_read_rejected")
        try:
            verify_stage_bundle(content, sha256=sha256, identity=context.identity, stage=stage, input_sha256=input_sha256)
        except ValueError:
            raise PipelineRunnerError("checkpoint_transport_bundle_invalid") from None
        self._guard.confirm()
        self.downloads += 1
        return content

    def upload(self, *, context: PipelineExecutionContext, stage: str, input_sha256: str, data: bytes, sha256: str) -> dict:
        with self._lock:
            return self._upload(context=context, stage=stage, input_sha256=input_sha256, data=data, sha256=sha256)

    def _upload(self, *, context: PipelineExecutionContext, stage: str, input_sha256: str, data: bytes, sha256: str) -> dict:
        params = self._params(context, stage, input_sha256, sha256)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_OBJECT_BYTES:
            raise PipelineRunnerError("checkpoint_transport_object_limit")
        verify_stage_bundle(data, sha256=sha256, identity=context.identity, stage=stage, input_sha256=input_sha256)
        try:
            status, content, _ = self._request("POST", params, data)
        except PipelineRunnerError as exc:
            if str(exc) != "checkpoint_transport_unavailable":
                raise
            status, content = 503, b""
        expected_key = self.key(context, stage, input_sha256, sha256)
        if status == 200:
            try:
                result = json.loads(content)
            except ValueError:
                raise PipelineRunnerError("checkpoint_transport_receipt_invalid") from None
            if not isinstance(result, dict) or result.get("decision") != "verified" or result.get("key") != expected_key or result.get("sha256") != sha256 or result.get("bytes") != len(data):
                raise PipelineRunnerError("checkpoint_transport_receipt_invalid")
            self._guard.confirm()
        elif status >= 500:
            # Exactly one bounded readback, never a blind second upload.
            if self.download(context=context, stage=stage, input_sha256=input_sha256, sha256=sha256) != data:
                raise PipelineRunnerError("checkpoint_transport_readback_mismatch")
        else:
            raise PipelineRunnerError("checkpoint_transport_write_rejected")
        self.unconfirmed_write_outcome = False
        return {"key": expected_key, "sha256": sha256, "bytes": len(data)}

    def evidence(self) -> dict:
        return {"requests": self.request_count, "maximumRequests": self._maximum, "uploadAttempts": self.upload_attempts,
                "verifiedDownloads": self.downloads, "bytesSent": self.bytes_sent, "bytesReceived": self.bytes_received,
                "unconfirmedWriteOutcome": self.unconfirmed_write_outcome, "d1RowsRead": None, "d1RowsWritten": 0,
                "accountRemaining": None, "resourceAllowance": "in_process_reference" if self._budget_ledger else "missing",
                "sharedDurableReservation": False}
