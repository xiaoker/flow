import copy
import hashlib
import json

import pytest

from trading_desk.pipeline_publication import build_candidate_upload_body
from trading_desk.read_models import canonical_json_bytes


def fixture():
    content = b'{}'
    manifest = {"schemaVersion": 4, "asOf": "2026-09-29", "producerBuildSha": "a" * 40,
        "coverage": {"fixtureRatio": 1.0}, "artifacts": {"overview": {
            "key": "fixture-key", "bytes": 2, "sha256": hashlib.sha256(content).hexdigest()}}}
    manifest["manifestSha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    execution = {"targetDate": manifest["asOf"], "producerBuildSha": manifest["producerBuildSha"],
        "runKey": "daily-market-data:2026-09-30:reconcile:v1", "leaseOwner": "fixture", "attemptCount": 1}
    return canonical_json_bytes(manifest), content, execution


def test_candidate_wire_preserves_exact_signed_bytes_and_does_not_mutate_identity():
    raw, content, execution = fixture(); before = copy.deepcopy(execution)
    body = build_candidate_upload_body(raw, "overview", content, execution=execution, finalize=True)
    assert body["candidateManifestJson"].encode() == raw
    assert b'"fixtureRatio":1.0' in raw
    assert body["pipelineExecution"] == execution == before
    assert body["activationMode"] == "stage_only" and body["finalize"] is True


@pytest.mark.parametrize("raw", [b"", b"x" * 65537, b"invalid", b"[]", b'{"schemaVersion":4,"schemaVersion":4}'])
def test_invalid_manifest_bytes_are_rejected(raw):
    _, content, execution = fixture()
    with pytest.raises(ValueError):
        build_candidate_upload_body(raw, "overview", content, execution=execution, finalize=False)


def test_hash_artifact_date_and_build_mismatch_rejected():
    raw, content, execution = fixture()
    for value in [raw.replace(b"1.0", b"2.0"), json.dumps(json.loads(raw), indent=2).encode()]:
        with pytest.raises(ValueError):
            build_candidate_upload_body(value, "overview", content, execution=execution, finalize=False)
    with pytest.raises(ValueError, match="artifact_identity"):
        build_candidate_upload_body(raw, "overview", b"wrong", execution=execution, finalize=False)
    for key, value in [("targetDate", "2026-09-30"), ("producerBuildSha", "b" * 40)]:
        with pytest.raises(ValueError, match="identity_mismatch"):
            build_candidate_upload_body(raw, "overview", content, execution=execution | {key: value}, finalize=False)


@pytest.mark.parametrize("content,finalize", [(b"", False), (b"x" * 1_500_001, False), (b"{}", "true")])
def test_candidate_content_and_finalize_are_bounded(content, finalize):
    raw, _, execution = fixture()
    with pytest.raises(ValueError, match="bytes_limit"):
        build_candidate_upload_body(raw, "overview", content, execution=execution, finalize=finalize)
