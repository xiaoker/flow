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


def test_large_repetitive_bundle_fits_unchanged_transport_and_restores(tmp_path):
    from trading_desk.pipeline_checkpoint_transport import MAX_OBJECT_BYTES
    root, checkpoint = fixture(tmp_path)
    content = b'{"officialDate":"2026-09-15","shares":123456}\n' * 70000
    (root / "input-snapshot.json").write_bytes(content)
    data, digest = build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    assert len(data) <= MAX_OBJECT_BYTES
    assert json.loads(data)["schemaVersion"] == 2
    assert (data, digest) == build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    restored_checkpoint, files = verify_stage_bundle(data, sha256=digest,
        identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    assert restored_checkpoint == checkpoint
    assert files["input-snapshot.json"] == content


@pytest.mark.parametrize("fault", ["size", "hash", "codec", "truncated", "deflate", "expanded_limit", "nested"])
def test_compressed_envelope_rejects_corruption_and_expansion(tmp_path, monkeypatch, fault):
    import base64
    import gzip
    import trading_desk.pipeline_checkpoint_bundle as module
    root, checkpoint = fixture(tmp_path)
    (root / "large.json").write_bytes(b"official-input" * 100000)
    data, _ = build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage="core_collection", input_sha256=INPUT)
    value = json.loads(data)
    assert value["schemaVersion"] == 2
    if fault == "size": value["decodedBytes"] -= 1
    elif fault == "hash": value["decodedSha256"] = "0" * 64
    elif fault == "codec": value["encoding"] = "unknown"
    elif fault == "truncated": value["data"] = base64.b64encode(base64.b64decode(value["data"])[:-8]).decode()
    elif fault == "deflate":
        compressed = bytearray(base64.b64decode(value["data"]))
        compressed[10] = (compressed[10] & 0xF8) | 0x07  # invalid DEFLATE block type
        value["data"] = base64.b64encode(compressed).decode()
    elif fault == "expanded_limit": monkeypatch.setattr(module, "MAX_BUNDLE_BYTES", len(data) + 1)
    else:
        nested = json.dumps(value).encode()
        value.update(decodedBytes=len(nested), decodedSha256=hashlib.sha256(nested).hexdigest(),
                     data=base64.b64encode(gzip.compress(nested, mtime=0)).decode())
    data = json.dumps(value).encode()
    with pytest.raises(CheckpointBundleError):
        verify_stage_bundle(data, sha256=hashlib.sha256(data).hexdigest(),
            identity=IDENTITY, stage="core_collection", input_sha256=INPUT)


def test_incompressible_bundle_still_rejected_at_original_wire_cap(tmp_path):
    import random
    root, checkpoint = fixture(tmp_path)
    (root / "large.bin").write_bytes(random.Random(42).randbytes(1100000))
    with pytest.raises(CheckpointBundleError, match="bundle_transport_size_exceeded"):
        build_stage_bundle(workspace=root, checkpoint=checkpoint,
            identity=IDENTITY, stage="core_collection", input_sha256=INPUT)


def nested_archive_stage(tmp_path):
    import base64
    import random
    root, checkpoint = fixture(tmp_path)
    rng = random.Random(42)
    csv = '\n'.join(f'{i%1000:06},{rng.randrange(10**10)},{rng.randrange(10**10)}'
                    for i in range(45000)).encode()
    content = json.dumps({'contentBase64': base64.b64encode(csv).decode()}, separators=(',', ':')).encode()
    (root / 'input-snapshot.json').write_bytes(content)
    return root, checkpoint, content


def test_nested_archive_compression_restores_all_bytes_in_new_process_directory(tmp_path):
    from trading_desk.pipeline_checkpoint_bundle import restore_stage_bundle
    from trading_desk.pipeline_checkpoint_transport import MAX_OBJECT_BYTES
    root, checkpoint, content = nested_archive_stage(tmp_path)
    data, digest = build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage='core_collection', input_sha256=INPUT)
    assert len(data) <= MAX_OBJECT_BYTES
    assert json.loads(data)['schemaVersion'] == 3
    assert (data, digest) == build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage='core_collection', input_sha256=INPUT)
    restored, files = verify_stage_bundle(data, sha256=digest, identity=IDENTITY,
        stage='core_collection', input_sha256=INPUT)
    assert restored == checkpoint
    assert files['input-snapshot.json'] == content
    destination = tmp_path / 'new-process'
    fences = []
    def verify_restored(**kwargs):
        return all((kwargs['workspace'] / name).read_bytes() == body for name, body in files.items())
    assert restore_stage_bundle(data, sha256=digest, identity=IDENTITY,
        stage='core_collection', input_sha256=INPUT, workspace=destination,
        confirm_lease=lambda: fences.append('checked'), verify_output=verify_restored) == checkpoint
    assert len(fences) == len(files) + 2


@pytest.mark.parametrize('fault', ['encoding', 'bytes', 'hash', 'truncated', 'expansion', 'path', 'duplicate'])
def test_file_compression_preserves_integrity_and_expansion_checks(tmp_path, fault):
    import base64
    import gzip
    root, checkpoint, _ = nested_archive_stage(tmp_path)
    data, _ = build_stage_bundle(workspace=root, checkpoint=checkpoint,
        identity=IDENTITY, stage='core_collection', input_sha256=INPUT)
    value = json.loads(data)
    assert value['schemaVersion'] == 3
    entry = value['files'][0]
    if fault == 'encoding': entry['encoding'] = 'unknown'
    elif fault == 'bytes': entry['bytes'] = 8000001
    elif fault == 'hash': entry['sha256'] = '0'*64
    elif fault == 'truncated': entry['data'] = entry['data'][:-8]
    elif fault == 'expansion':
        entry['bytes'] = 1
        entry['data'] = base64.b64encode(gzip.compress(b'x'*100000, mtime=0)).decode()
    elif fault == 'path': entry['path'] = '../outside'
    else: value['files'].append(dict(entry))
    value['bundleManifestSha256'] = canonical_sha256({k:v for k,v in value.items() if k!='bundleManifestSha256'})
    data = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(CheckpointBundleError):
        verify_stage_bundle(data, sha256=hashlib.sha256(data).hexdigest(), identity=IDENTITY,
            stage='core_collection', input_sha256=INPUT)


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
