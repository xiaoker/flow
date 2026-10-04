"""Bounded ordinary candidate publication, never activation or scheduling.

Uses the existing upload wire contract, lease guard, shared allowance and HTTP
exchange. A lost intermediate upload response cannot be read through the
existing STAGED-only API: keep it ambiguous and never blindly retry it.
This is an injected transport, not a production runner/CLI registration.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import urlparse

from .bounded_http import BoundedHTTPExchange, BoundedHTTPError
from .http import HttpSettings, build_session
from .pipeline_publication import build_candidate_upload_body
from .pipeline_publication_intent import PublicationIntentStore
from .pipeline_runner import PipelineExecutionContext, PipelineRunnerError
from .resource_budget import BudgetLedger, BudgetVector
from .resource_budget_remote import SharedBudgetLedger

MAX_REQUEST_SECONDS = 20
MAX_RESPONSE_BYTES = 1_500_000


class CandidatePublicationTransport:
    def __init__(self, *, base_url: str, ingest_key: str, access_client_id: str,
                 access_client_secret: str, lease_guard: Any, budget_ledger: BudgetLedger,
                 budget_task_id: str, request_costs: dict[str, BudgetVector],
                 maximum_requests: int = 40, session: Any = None, monotonic=time.monotonic,
                 allow_loopback_fixture: bool = False, intent_store: PublicationIntentStore | None = None):
        parsed = urlparse(base_url)
        fixture = allow_loopback_fixture and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if not ((parsed.scheme == "https" and parsed.hostname == "flow.xiaoker.com" and parsed.port in {None, 443}) or fixture) or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise PipelineRunnerError("candidate_transport_origin_invalid")
        if any(not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value
               for value in (ingest_key, access_client_id, access_client_secret)):
            raise PipelineRunnerError("candidate_transport_credentials_required")
        if type(maximum_requests) is not int or not 1 <= maximum_requests <= 40:
            raise PipelineRunnerError("candidate_transport_budget_invalid")
        self._session = session if session is not None else build_session(HttpSettings(total_retries=0))
        if any(getattr(getattr(adapter, "max_retries", None), "total", None) not in {0, False}
               for adapter in getattr(self._session, "adapters", {}).values()):
            raise PipelineRunnerError("candidate_transport_hidden_retries_forbidden")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {ingest_key}", "CF-Access-Client-Id": access_client_id,
                         "CF-Access-Client-Secret": access_client_secret, "Content-Type": "application/json"}
        self._guard, self._ledger, self._task = lease_guard, budget_ledger, budget_task_id
        self._costs, self._maximum = dict(request_costs), maximum_requests
        self._exchange = BoundedHTTPExchange(self._session, monotonic=monotonic)
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self._intent_store = intent_store
        self.request_count = self.upload_attempts = self.readbacks = 0
        self.bytes_sent = self.bytes_received = self._shared_authorizations = 0
        self.response_measurement_complete = True

    @staticmethod
    def execution(context: PipelineExecutionContext) -> dict:
        if context.mode != "ordinary" or context.recovery_id is not None or context.lease_attempt_count is None or context.identity.target_date is None:
            raise PipelineRunnerError("candidate_transport_requires_bound_ordinary_lease")
        return {"runKey": context.identity.run_key, "targetDate": context.identity.target_date,
                "producerBuildSha": context.identity.producer_build_sha,
                "leaseOwner": context.lease_owner, "attemptCount": context.lease_attempt_count}

    def _authorize(self, method: str, execution: dict, body: dict | None) -> None:
        cost = self._costs.get(method)
        if not isinstance(self._ledger, BudgetLedger) or not self._task or not isinstance(cost, BudgetVector) or not cost.all_known():
            raise PipelineRunnerError("candidate_resource_allowance_required")
        if isinstance(self._ledger, SharedBudgetLedger):
            try:
                self._ledger.validate_execution_identity(run_key=execution["runKey"], lease_owner=execution["leaseOwner"],
                    attempt_count=execution["attemptCount"], target_date=execution["targetDate"], task_id=self._task)
            except (ValueError, KeyError):
                raise PipelineRunnerError("candidate_shared_budget_identity_mismatch") from None
        # Floors are endpoint operations, NOT provider measurements. The caller
        # must supply all dimensions plus lease/control overhead and task bounds.
        floors = {"workerRequests": 1, "d1RowsRead": 1, "r2ClassB": 1}
        if body is not None:
            final = body["finalize"]
            floors.update(d1RowsRead=10 if final else 3, d1RowsWritten=1 if final else 0,
                r2ClassA=2 if final else 1,
                r2ClassB=(2 + len(body["manifest"]["artifacts"])) if final else 1,
                r2Bytes=body["artifact"]["bytes"] + (len(body["candidateManifestJson"].encode()) if final else 0))
        if any(cost.metrics[key].value < minimum for key, minimum in floors.items()):
            raise PipelineRunnerError("candidate_resource_cost_understated")
        try:
            admitted = self._ledger.authorize_step(self._task, cost)
        except ValueError:
            raise PipelineRunnerError("candidate_resource_allowance_rejected") from None
        if admitted.get("decision") != "authorized":
            raise PipelineRunnerError("candidate_resource_allowance_rejected")
        if isinstance(self._ledger, SharedBudgetLedger):
            self._shared_authorizations += 1

    def _request(self, method: str, path: str, *, execution: dict, body: dict | None = None,
                 params: dict | None = None) -> Any:
        try:
            self._exchange.ensure_idle()
        except BoundedHTTPError:
            raise PipelineRunnerError("candidate_transport_request_still_unconfirmed") from None
        if self.request_count >= self._maximum:
            raise PipelineRunnerError("candidate_transport_budget_exceeded")
        self._authorize(method, execution, body)
        self._guard.confirm()
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() if body else None
        if body is not None and self._intent_store is not None:
            self._intent_store.begin(self._reference(body))
        self.request_count += 1
        if body is not None:
            self.upload_attempts += 1
            self.bytes_sent += len(data)
            self._pending = {"execution": execution, "manifest": body["manifest"],
                             "name": body["artifact"]["name"], "finalize": body["finalize"]}
        try:
            reply = self._exchange.request(method, self._base + path, params=params, data=data,
                headers=self._headers, max_bytes=MAX_RESPONSE_BYTES, chunk_size=65536,
                max_chunks=64, deadline_seconds=MAX_REQUEST_SECONDS)
        except BoundedHTTPError:
            self.response_measurement_complete = False
            raise PipelineRunnerError("candidate_transport_unavailable") from None
        self.bytes_received += len(reply.content)
        return reply

    @staticmethod
    def _reference(body: dict) -> dict:
        artifact = body["artifact"]
        return {"execution": body["pipelineExecution"], "releaseId": body["manifest"]["releaseId"],
                "manifestSha256": body["manifest"]["manifestSha256"],
                "name": artifact["name"], "finalize": body["finalize"],
                "artifactSha256": artifact["sha256"], "artifactBytes": artifact["bytes"],
                "artifactKey": artifact["key"]}

    @staticmethod
    def _receipt(body: dict) -> dict:
        manifest = body["manifest"]
        result = {"status": "staged_candidate" if body["finalize"] else "staged", "asOf": manifest["asOf"]}
        if body["finalize"]:
            result.update(releaseId=manifest["releaseId"],
                manifestKey=f'market-data/releases/{manifest["asOf"]}/{manifest["releaseId"]}/manifest.json')
        else:
            result["name"] = body["artifact"]["name"]
        return result

    def upload(self, *, context: PipelineExecutionContext, manifest_bytes: bytes,
               name: str, content: bytes, finalize: bool) -> dict:
        with self._lock:
            if self._pending is not None:
                raise PipelineRunnerError("candidate_unconfirmed_write_requires_reconciliation")
            execution = self.execution(context)
            body = build_candidate_upload_body(manifest_bytes, name, content, execution=execution, finalize=finalize)
            manifest = body["manifest"]
            if not isinstance(manifest.get("releaseId"), str) or not 1 <= len(manifest["artifacts"]) <= 20:
                raise PipelineRunnerError("candidate_transport_manifest_invalid")
            expected = self._receipt(body)
            if self._intent_store is not None:
                recorded = self._intent_store.lookup(self._reference(body))
                if recorded is not None:
                    if recorded["status"] != "ACKNOWLEDGED":
                        raise PipelineRunnerError("candidate_unconfirmed_write_requires_reconciliation")
                    if recorded["receipt"] != expected:
                        raise PipelineRunnerError("candidate_transport_receipt_invalid")
                    self._guard.confirm()
                    return expected
            reply = self._request("POST", "/api/v1/admin/read-model-import", execution=execution, body=body)
            if reply.status != 200:
                raise PipelineRunnerError("candidate_transport_write_rejected")
            try:
                result = json.loads(reply.content)
            except ValueError:
                raise PipelineRunnerError("candidate_transport_receipt_invalid") from None
            if not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items()):
                raise PipelineRunnerError("candidate_transport_receipt_invalid")
            self._guard.confirm()
            if self._intent_store is not None:
                self._intent_store.acknowledge(self._reference(body), expected)
            self._pending = None
            return expected

    def reconcile_final_upload(self, *, context: PipelineExecutionContext, manifest_bytes: bytes,
                               name: str, content: bytes) -> dict:
        """One explicit exact readback; no automatic retry or orphan bypass.

        Only the same final upload identity can clear a pending write. A 404 or
        failed read proves nothing about an earlier R2 write and stays unknown.
        """
        with self._lock:
            execution = self.execution(context)
            body = build_candidate_upload_body(manifest_bytes, name, content, execution=execution, finalize=True)
            manifest = body["manifest"]
            expected_pending = {"execution": execution, "manifest": manifest, "name": name, "finalize": True}
            if self._intent_store is not None:
                recorded = self._intent_store.lookup(self._reference(body))
                if recorded is None or recorded["status"] != "PENDING":
                    raise PipelineRunnerError("candidate_reconciliation_identity_mismatch")
                if self._pending is None:
                    self._pending = expected_pending
            if self._pending != expected_pending:
                raise PipelineRunnerError("candidate_reconciliation_identity_mismatch")
            reply = self._request("GET", "/api/v1/admin/candidate-release", execution=execution,
                params={"releaseId": manifest["releaseId"], "manifestSha256": manifest["manifestSha256"], "artifact": name})
            headers = {str(k).lower(): v for k, v in reply.headers.items()}
            expected_headers = {"x-flow-release-id": manifest["releaseId"], "x-flow-as-of": manifest["asOf"],
                "x-flow-release-status": "STAGED", "x-flow-manifest-sha256": manifest["manifestSha256"],
                "x-flow-artifact": name, "x-flow-artifact-sha256": hashlib.sha256(content).hexdigest(),
                "x-flow-artifact-bytes": str(len(content))}
            if reply.status != 200 or any(headers.get(k) != v for k, v in expected_headers.items()) or reply.content != content:
                raise PipelineRunnerError("candidate_transport_readback_mismatch")
            self._guard.confirm()
            self.readbacks += 1
            if self._intent_store is not None:
                self._intent_store.acknowledge(self._reference(body), self._receipt(body))
            self._pending = None
            return {"status": "staged_candidate", "releaseId": manifest["releaseId"],
                    "asOf": manifest["asOf"], "manifestSha256": manifest["manifestSha256"]}

    def evidence(self) -> dict:
        shared = isinstance(self._ledger, SharedBudgetLedger)
        return {"requests": self.request_count, "maximumRequests": self._maximum,
                "durableLocalIntent": self._intent_store is not None,
                "uploadAttempts": self.upload_attempts, "verifiedReadbacks": self.readbacks,
                "bytesSent": self.bytes_sent, "bytesReceived": self.bytes_received,
                "unconfirmedWriteOutcome": self._pending is not None or (self._intent_store is not None and self._intent_store.has_pending()),
                "responseMeasurementComplete": self.response_measurement_complete,
                "bytesReceivedBasis": "measured" if self.response_measurement_complete else "measured_lower_bound",
                "d1RowsRead": None, "d1RowsWritten": None, "accountRemaining": None,
                "resourceAllowance": "server_authoritative_shared" if shared else "in_process_reference",
                "sharedAuthorizations": self._shared_authorizations,
                "budgetControlPlane": self._ledger.accounting_evidence() if shared else None}
