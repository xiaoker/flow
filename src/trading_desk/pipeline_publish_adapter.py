"""Injected ordinary STAGED publication adapter; never activation or scheduling."""
from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .pipeline_adapters import PipelineAdapterError, _atomic_json
from .pipeline_application import canonical_sha256
from .pipeline_build_adapter import _read
from .pipeline_candidate_transport import CandidatePublicationTransport
from .pipeline_publication import build_candidate_upload_body
from .pipeline_verify_adapter import CoreReadModelVerifyAdapter, RECEIPT_LIMIT
from .resource_budget_remote import SharedBudgetLedger


@dataclass(frozen=True)
class CoreCandidatePublishAdapter:
    source_dir: Path
    transport: CandidatePublicationTransport
    execution_context: object
    restore_completed_intents: bool = False
    allowed_side_effects: tuple[str, ...] = (
        "filesystem:isolated_stage_workspace", "candidate_publication_requests")

    def _sources(self, context, config, input_sha256):
        if set(config) != {"verifyInputSha256", "verifyOutputSha256"} or canonical_sha256(config) != input_sha256:
            raise PipelineAdapterError("publish_input_contract_mismatch")
        if not isinstance(self.transport, CandidatePublicationTransport):
            raise PipelineAdapterError("publish_transport_required")
        if not isinstance(self.transport._ledger, SharedBudgetLedger):
            raise PipelineAdapterError("publish_shared_budget_required")
        execution = self.transport.execution(context)
        if execution != self.transport.execution(self.execution_context):
            raise PipelineAdapterError("publish_execution_binding_mismatch")
        checkpoint = {"identity": context.identity.as_dict(), "stage": "verify", "status": "COMPLETED",
            "inputSha256": config["verifyInputSha256"], "outputSha256": config["verifyOutputSha256"]}
        source_dir = Path(self.source_dir)
        if not CoreReadModelVerifyAdapter(source_dir).verify_checkpoint(
                checkpoint=checkpoint, context=context, workspace=source_dir):
            raise PipelineAdapterError("publish_verified_source_invalid")
        directory = source_dir / "build"
        manifest_bytes = _read(directory / "manifest.json", RECEIPT_LIMIT)
        manifest = json.loads(manifest_bytes)
        names = sorted(manifest["artifacts"])
        if not 1 <= len(names) <= 20:
            raise PipelineAdapterError("publish_artifact_count_invalid")
        sources = []
        # Validate every exact body and local binding before any remote effect.
        for index, name in enumerate(names):
            content = _read(directory / f"{name}.json", 1_500_000)
            body = build_candidate_upload_body(manifest_bytes, name, content,
                execution=execution, finalize=index == len(names) - 1)
            self.transport.require_durable_intent(body)
            sources.append((dict(context=context, manifest_bytes=manifest_bytes, name=name, content=content), body))
        return manifest, sources

    def _result(self, manifest, input_sha256):
        evidence = {"asOf": manifest["asOf"], "releaseId": manifest["releaseId"],
            "manifestSha256": manifest["manifestSha256"], "candidateStatus": "STAGED",
            "artifactCount": len(manifest["artifacts"]), "allArtifactsReadbackVerified": True,
            "activeReleaseChanged": False, "sourceRequestCount": 0}
        return {"status": "COMPLETED", "outputSha256": canonical_sha256({
            "inputSha256": input_sha256, "evidence": evidence}),
            "allowedSideEffects": list(self.allowed_side_effects), "evidence": evidence}

    def run(self, *, context, config, input_sha256, workspace):
        from .pipeline_runner import _assert_safe_output
        workspace = Path(workspace)
        receipt_path = workspace / "adapter-receipt.json"
        _assert_safe_output(receipt_path)
        manifest, sources = self._sources(context, config, input_sha256)
        if receipt_path.exists():
            receipt = json.loads(_read(receipt_path, RECEIPT_LIMIT))
            checkpoint = {"identity": context.identity.as_dict(), "stage": "publish", "status": "COMPLETED",
                "inputSha256": input_sha256, "outputSha256": receipt.get("stageResult", {}).get("outputSha256")}
            if not self.verify_checkpoint(context=context, checkpoint=checkpoint, workspace=workspace):
                raise PipelineAdapterError("publish_checkpoint_invalid")
            return receipt["stageResult"]
        # The injected transport's retained intent file lives outside this
        # stage directory; this adapter never initializes or resets it.
        if workspace.exists() and next(workspace.iterdir(), None) is not None:
            raise PipelineAdapterError("partial_publish_workspace_requires_reconciliation")
        required = len(sources)
        for _source, body in sources:
            recorded = self.transport.require_durable_intent(body)
            if recorded is None or recorded["status"] == "PENDING":
                required += 1
        self.transport.require_request_capacity(required)
        # Resolve the one unknown write before replaying earlier local
        # acknowledgements: the transport intentionally blocks all uploads
        # while its instance-local unknown hold remains set.
        for source, body in sources:
            recorded = self.transport.require_durable_intent(body)
            if recorded is not None and recorded["status"] == "PENDING":
                if body["finalize"]:
                    self.transport.reconcile_final_upload(**source)
                else:
                    self.transport.reconcile_intermediate_upload(**source)
        for source, body in sources:
            self.transport.upload(**source, finalize=body["finalize"])
        for source, _body in sources:
            self.transport.verify_staged_artifact(**source)
        result = self._result(manifest, input_sha256)
        _atomic_json(receipt_path, {"schemaVersion": 1, "identity": context.identity.as_dict(),
            "inputSha256": input_sha256, "config": dict(config), "stageResult": result})
        return result

    def verify_checkpoint(self, *, checkpoint, workspace, context=None, identity=None, input_sha256=None):
        # Remote errors propagate rather than blessing a local checkpoint.
        context = context if context is not None else self.execution_context
        if identity is not None and identity != context.identity:
            return False
        if input_sha256 is not None and checkpoint.get("inputSha256") != input_sha256:
            return False
        if (checkpoint.get("identity") != context.identity.as_dict() or checkpoint.get("stage") != "publish"
                or checkpoint.get("status") != "COMPLETED"):
            return False
        workspace = Path(workspace)
        if any(path.is_symlink() for path in (workspace, *workspace.parents)):
            return False
        if not workspace.is_dir() or {path.name for path in islice(workspace.iterdir(), 3)} != {"adapter-receipt.json"}:
            return False
        receipt = json.loads(_read(workspace / "adapter-receipt.json", RECEIPT_LIMIT))
        if receipt.get("schemaVersion") != 1 or receipt.get("identity") != context.identity.as_dict() or receipt.get("inputSha256") != checkpoint.get("inputSha256"):
            return False
        manifest, sources = self._sources(context, receipt["config"], checkpoint["inputSha256"])
        result = self._result(manifest, checkpoint["inputSha256"])
        if receipt.get("stageResult") != result or checkpoint.get("outputSha256") != result["outputSha256"]:
            return False
        self.transport.require_request_capacity(len(sources))
        if self.restore_completed_intents:
            # A completed checkpoint is a locator, never an upload permission.
            # Rebuild only current-owner local acknowledgements from fresh,
            # exact STAGED reads. Validate every local record before any I/O,
            # then every remote artifact before persisting the first record.
            missing = []
            for source, body in sources:
                recorded = self.transport.require_durable_intent(body)
                if recorded is None:
                    missing.append(body)
                elif recorded["status"] != "ACKNOWLEDGED" or recorded["receipt"] != self.transport._receipt(body):
                    return False
            for source, _body in sources:
                self.transport.verify_staged_artifact(**source)
            for body in missing:
                self.transport._guard.confirm()
                reference = self.transport._reference(body)
                self.transport._intent_store.begin(reference)
                self.transport._intent_store.acknowledge(reference, self.transport._receipt(body))
            return True
        for source, body in sources:
            recorded = self.transport.require_durable_intent(body)
            if recorded is None or recorded["status"] != "ACKNOWLEDGED" or recorded["receipt"] != self.transport._receipt(body):
                return False
            self.transport.verify_staged_artifact(**source)
        return True
