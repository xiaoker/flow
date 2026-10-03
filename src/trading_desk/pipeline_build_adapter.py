"""Candidate offline core builder; no production registration or publication."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .pipeline_adapters import PipelineAdapterError, _atomic_json
from .pipeline_application import canonical_sha256
from .read_model_capabilities import CAPABILITY_CONTRACT_VERSION
from .read_models import build_market_read_models, canonical_json_bytes

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_FILES = 32


def _result(encoded, manifest):
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in encoded.items()}
    return {"status": "COMPLETED", "outputSha256": canonical_sha256(hashes),
            "allowedSideEffects": ["filesystem:isolated_stage_workspace"],
            "evidence": {"asOf": manifest["asOf"], "releaseId": manifest["releaseId"],
                "producerBuildSha": manifest["producerBuildSha"], "artifactHashes": hashes,
                "outputBytes": sum(map(len, encoded.values())), "sourceRequestCount": 0,
                "publicationPerformed": False}}


def _read(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PipelineAdapterError("build_input_or_output_missing")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PipelineAdapterError("build_artifact_size_exceeded")
    return data


@dataclass(frozen=True)
class CoreReadModelBuildAdapter:
    snapshot_path: Path
    allowed_side_effects: tuple[str, ...] = ("filesystem:isolated_stage_workspace",)

    def _expected(self, identity, config, input_sha256, data):
        if set(config) != {"snapshotSha256"} or canonical_sha256(config) != input_sha256:
            raise PipelineAdapterError("build_input_contract_mismatch")
        if hashlib.sha256(data).hexdigest() != config["snapshotSha256"]:
            raise PipelineAdapterError("build_snapshot_hash_mismatch")
        try:
            snapshot = json.loads(data)
        except ValueError:
            raise PipelineAdapterError("build_snapshot_invalid") from None
        if not isinstance(snapshot, dict) or snapshot.get("asOf") != identity.target_date:
            raise PipelineAdapterError("build_snapshot_date_mismatch")
        payloads, manifest = build_market_read_models(snapshot, require_materialized=True,
            producer_build_sha=identity.producer_build_sha,
            capability_contract_version=CAPABILITY_CONTRACT_VERSION)
        encoded = {f"{name}.json": canonical_json_bytes(value) for name, value in payloads.items()}
        encoded["manifest.json"] = canonical_json_bytes(manifest)
        if len(encoded) + 2 > MAX_FILES or sum(map(len, encoded.values())) > MAX_OUTPUT_BYTES:
            raise PipelineAdapterError("build_output_budget_exceeded")
        return encoded, manifest

    def run(self, *, context, input_sha256, config, workspace):
        from .pipeline_runner import _assert_safe_output
        workspace = Path(workspace)
        _assert_safe_output(workspace / "adapter-receipt.json")
        if any(path.is_symlink() for path in (workspace, *workspace.parents)):
            raise PipelineAdapterError("build_workspace_symlink")
        receipt_path = workspace / "adapter-receipt.json"
        if receipt_path.exists():
            receipt = json.loads(_read(receipt_path, 65536))
            if not self.verify_checkpoint(context=context, checkpoint={"inputSha256": input_sha256,
                    "identity": context.identity.as_dict(), "stage": "build", "status": "COMPLETED",
                    "outputSha256": receipt.get("stageResult", {}).get("outputSha256")}, workspace=workspace):
                raise PipelineAdapterError("build_checkpoint_invalid")
            if receipt.get("config") != dict(config):
                raise PipelineAdapterError("build_input_contract_mismatch")
            return receipt["stageResult"]
        if workspace.exists() and next(workspace.iterdir(), None) is not None:
            raise PipelineAdapterError("partial_build_requires_reconciliation")
        if context.lease_check is not None:
            context.lease_check()
        data = _read(self.snapshot_path, MAX_INPUT_BYTES)
        encoded, manifest = self._expected(context.identity, config, input_sha256, data)
        if context.lease_check is not None:
            context.lease_check()
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "input-snapshot.json").write_bytes(data)
        for name, content in encoded.items():
            (workspace / name).write_bytes(content)
        result = _result(encoded, manifest)
        _atomic_json(receipt_path, {"schemaVersion": 1, "identity": context.identity.as_dict(),
                    "inputSha256": input_sha256, "config": dict(config), "stageResult": result})
        return result

    def verify_checkpoint(self, *, checkpoint, workspace, identity=None, input_sha256=None, context=None):
        try:
            identity = context.identity if context is not None else identity
            if checkpoint.get("identity") != identity.as_dict() or checkpoint.get("stage") != "build" or checkpoint.get("status") != "COMPLETED":
                return False
            workspace = Path(workspace)
            if any(path.is_symlink() for path in (workspace, *workspace.parents)):
                return False
            receipt = json.loads(_read(workspace / "adapter-receipt.json", 65536))
            if receipt.get("schemaVersion") != 1 or receipt.get("identity") != identity.as_dict():
                return False
            input_sha = checkpoint.get("inputSha256")
            if input_sha256 is not None and input_sha256 != input_sha:
                return False
            if receipt.get("inputSha256") != input_sha:
                return False
            data = _read(workspace / "input-snapshot.json", MAX_INPUT_BYTES)
            encoded, manifest = self._expected(identity, receipt["config"], input_sha, data)
            if receipt.get("stageResult") != _result(encoded, manifest):
                return False
            names = {p.name for p in islice(workspace.iterdir(), MAX_FILES + 1)}
            if names != set(encoded) | {"input-snapshot.json", "adapter-receipt.json"}:
                return False
            for name, expected in encoded.items():
                if _read(workspace / name, MAX_OUTPUT_BYTES) != expected:
                    return False
            digest = canonical_sha256({name: hashlib.sha256(value).hexdigest() for name, value in encoded.items()})
            return digest == checkpoint.get("outputSha256") == receipt["stageResult"]["outputSha256"]
        except (ValueError, KeyError, TypeError, AttributeError, OSError, PipelineAdapterError):
            return False
