import hashlib
import json

import pytest

from trading_desk.pipeline_application import ApplicationIdentity, build_checkpoint, canonical_sha256
from trading_desk.pipeline_checkpoint_bundle import CheckpointBundleError, build_stage_bundle, verify_stage_bundle


IDENTITY = ApplicationIdentity("daily-market-data:2026-09-16:reconcile:v1", "2026-09-16", "2026-09-15", producer_build_sha="a" * 40)
INPUT = "b" * 64


def fixture(tmp_path):
    root = tmp_path / "stage"
    root.mkdir()
    (root / "adapter-receipt.json").write_text('{"verified":true}')
    (root / "data").mkdir()
    (root / "data" / "source.csv").write_bytes(b"code,shares\n600522,123\n")
    checkpoint = build_checkpoint(identity=IDENTITY, stage="core_collection", status="COMPLETED", input_sha256=INPUT, output_sha256="c" * 64)
    return root, checkpoint


def test_deterministic_complete_bundle_and_separate_environment(tmp_path):
    root, checkpoint = fixture(tmp_path)
    first = build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    assert first == build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    verified, files = verify_stage_bundle(first[0], sha256=first[1], identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    assert verified == checkpoint
    assert files == {"adapter-receipt.json": b'{"verified":true}', "data/source.csv": b"code,shares\n600522,123\n"}
    assert not (tmp_path / "other-machine").exists()  # verifier has no filesystem effects


@pytest.mark.parametrize("change", ["digest", "input", "identity", "stage"])
def test_wrong_contract_rejected(tmp_path, change):
    root, checkpoint = fixture(tmp_path)
    data, digest = build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    identity = IDENTITY if change != "identity" else ApplicationIdentity(IDENTITY.run_key, IDENTITY.window_date, "2026-09-14", producer_build_sha="a" * 40)
    with pytest.raises(CheckpointBundleError):
        verify_stage_bundle(data, sha256="0" * 64 if change == "digest" else digest, identity=identity, stage="build" if change == "stage" else "core_collection", input_sha256="d" * 64 if change == "input" else INPUT)


@pytest.mark.parametrize("change", ["traversal", "duplicate", "content", "protected", "file_dir_conflict"])
def test_untrusted_manifest_rejected_even_with_valid_outer_hash(tmp_path, change):
    root, checkpoint = fixture(tmp_path)
    data, _ = build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    value = json.loads(data)
    if change == "traversal":
        value["files"][0]["path"] = "../outside"
    elif change == "duplicate":
        value["files"].append(value["files"][0])
    elif change == "content":
        value["files"][0]["sha256"] = "0" * 64
    elif change == "protected":
        value["files"][0]["path"] = "memory/secret"
    else:
        value["files"][0]["path"] = "data"
    value["bundleManifestSha256"] = canonical_sha256({k: v for k, v in value.items() if k != "bundleManifestSha256"})
    data = json.dumps(value).encode()
    with pytest.raises(CheckpointBundleError):
        verify_stage_bundle(data, sha256=hashlib.sha256(data).hexdigest(), identity=IDENTITY, stage="core_collection", input_sha256=INPUT)


def test_symlink_and_missing_proof_rejected(tmp_path):
    root, checkpoint = fixture(tmp_path)
    (root / "link").symlink_to(root / "data/source.csv")
    with pytest.raises(CheckpointBundleError, match="symlink"):
        build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    checkpoint.pop("checkpointSha256")
    with pytest.raises(CheckpointBundleError):
        build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)


def test_hard_file_and_byte_limits(tmp_path, monkeypatch):
    import trading_desk.pipeline_checkpoint_bundle as module
    root, checkpoint = fixture(tmp_path)
    monkeypatch.setattr(module, "MAX_FILES", 1)
    with pytest.raises(CheckpointBundleError):
        build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    monkeypatch.setattr(module, "MAX_FILES", 256)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 1)
    with pytest.raises(CheckpointBundleError, match="byte_limit"):
        build_stage_bundle(workspace=root, checkpoint=checkpoint, identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
