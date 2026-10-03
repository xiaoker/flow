"""Offline core candidate verification; no publication or production wiring."""
from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .pipeline_adapters import PipelineAdapterError, _atomic_json
from .pipeline_application import canonical_sha256
from .pipeline_build_adapter import CoreReadModelBuildAdapter, MAX_FILES, MAX_OUTPUT_BYTES, MAX_INPUT_BYTES, _read

RECEIPT_LIMIT = 65536
MAX_SAVED_BYTES = MAX_OUTPUT_BYTES + MAX_INPUT_BYTES + RECEIPT_LIMIT


def _verified_build(directory, identity, config, input_sha256):
    if set(config) != {"buildInputSha256", "buildOutputSha256"} or canonical_sha256(config) != input_sha256:
        raise PipelineAdapterError("verify_input_contract_mismatch")
    checkpoint = {"identity": identity.as_dict(), "stage": "build", "status": "COMPLETED",
        "inputSha256": config["buildInputSha256"], "outputSha256": config["buildOutputSha256"]}
    # Recompute through the existing builder, including saved historical inputs,
    # capability/date/coverage rules and the exact file inventory. No second QA.
    if not CoreReadModelBuildAdapter(directory / "input-snapshot.json").verify_checkpoint(
            checkpoint=checkpoint, identity=identity, workspace=directory):
        raise PipelineAdapterError("verify_build_checkpoint_invalid")
    manifest = json.loads(_read(directory / "manifest.json", RECEIPT_LIMIT))
    if not isinstance(manifest.get("publication"), dict) or manifest["publication"].get("status") != "confirmed":
        raise PipelineAdapterError("verify_publication_not_confirmed")
    files = {}
    total = 0
    for path in islice(directory.iterdir(), MAX_FILES + 1):
        content = _read(path, MAX_SAVED_BYTES - total)
        total += len(content)
        files[path.name] = content
    if len(files) > MAX_FILES or total > MAX_SAVED_BYTES:
        raise PipelineAdapterError("verify_input_budget_exceeded")
    result = {"status": "COMPLETED", "outputSha256": canonical_sha256({
            "buildOutputSha256": config["buildOutputSha256"], "inputSha256": input_sha256}),
        "allowedSideEffects": ["filesystem:isolated_stage_workspace"],
        "evidence": {"asOf": manifest["asOf"], "releaseId": manifest["releaseId"],
            "buildOutputSha256": config["buildOutputSha256"], "verifiedBytes": total,
            "artifactCount": len(manifest["artifacts"]), "publicationPerformed": False,
            "sourceRequestCount": 0}}
    return files, result


@dataclass(frozen=True)
class CoreReadModelVerifyAdapter:
    source_dir: Path
    allowed_side_effects: tuple[str, ...] = ("filesystem:isolated_stage_workspace",)

    def run(self, *, context, input_sha256, config, workspace):
        from .pipeline_runner import _assert_safe_output
        workspace = Path(workspace)
        _assert_safe_output(workspace / "adapter-receipt.json")
        if any(path.is_symlink() for path in (workspace, *workspace.parents)):
            raise PipelineAdapterError("verify_workspace_symlink")
        receipt_path = workspace / "adapter-receipt.json"
        if receipt_path.exists():
            receipt = json.loads(_read(receipt_path, RECEIPT_LIMIT))
            checkpoint = {"identity": context.identity.as_dict(), "stage": "verify",
                "status": "COMPLETED", "inputSha256": input_sha256,
                "outputSha256": receipt.get("stageResult", {}).get("outputSha256")}
            if receipt.get("config") != dict(config) or not self.verify_checkpoint(
                    context=context, checkpoint=checkpoint, workspace=workspace):
                raise PipelineAdapterError("verify_checkpoint_invalid")
            return receipt["stageResult"]
        if workspace.exists() and next(workspace.iterdir(), None) is not None:
            raise PipelineAdapterError("partial_verify_requires_reconciliation")
        if context.lease_check is not None:
            context.lease_check()
        files, result = _verified_build(Path(self.source_dir), context.identity, config, input_sha256)
        if context.lease_check is not None:
            context.lease_check()
        saved = workspace / "build"
        saved.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (saved / name).write_bytes(content)
        # Validate bytes copied after the source check, not merely the earlier
        # filesystem observation; a concurrent source change cannot be blessed.
        _saved_files, saved_result = _verified_build(saved, context.identity, config, input_sha256)
        if saved_result != result:
            raise PipelineAdapterError("verify_saved_build_changed")
        if context.lease_check is not None:
            context.lease_check()
        _atomic_json(receipt_path, {"schemaVersion": 1, "identity": context.identity.as_dict(),
            "inputSha256": input_sha256, "config": dict(config), "stageResult": result})
        return result

    def verify_checkpoint(self, *, checkpoint, workspace, identity=None, input_sha256=None, context=None):
        try:
            identity = context.identity if context is not None else identity
            if checkpoint.get("identity") != identity.as_dict() or checkpoint.get("stage") != "verify" or checkpoint.get("status") != "COMPLETED":
                return False
            workspace = Path(workspace)
            if any(path.is_symlink() for path in (workspace, *workspace.parents)):
                return False
            if {p.name for p in islice(workspace.iterdir(), 3)} != {"build", "adapter-receipt.json"}:
                return False
            receipt = json.loads(_read(workspace / "adapter-receipt.json", RECEIPT_LIMIT))
            digest = checkpoint.get("inputSha256")
            if (receipt.get("schemaVersion") != 1 or receipt.get("identity") != identity.as_dict()
                    or receipt.get("inputSha256") != digest
                    or (input_sha256 is not None and input_sha256 != digest)):
                return False
            _files, result = _verified_build(workspace / "build", identity, receipt["config"], digest)
            return receipt.get("stageResult") == result and checkpoint.get("outputSha256") == result["outputSha256"]
        except (ValueError, KeyError, TypeError, AttributeError, OSError, PipelineAdapterError):
            return False
