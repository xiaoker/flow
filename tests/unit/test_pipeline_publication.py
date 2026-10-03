import base64
import copy
import gzip
import hashlib
import json

import pytest

from scripts import publish_read_models as script
from test_publish_read_models import _manifest
from trading_desk.pipeline_publication import build_upload_body, validate_publication_capabilities


def capabilities():
    return {"readModelManifestSchemas": [3, 4], "schemaVersions": [3, 4],
            "pressureSchemaVersions": [2], "stockShardCounts": [8],
            "features": ["requiredArtifacts", "sameDateObservedAtOrder", "releaseSlots",
                "bseMarginSeparateScope", "previousCompatibleRelease", "radarFlowView",
                "sourceReadinessLedger", "capabilityContractV07"]}


@pytest.mark.parametrize("mode,schema,finalize", [("active", 3, False), ("active", 3, True),
                                                  ("stage_only", 3, True), ("stage_only", 4, True)])
def test_shared_upload_preserves_existing_wire_contract(monkeypatch, mode, schema, finalize):
    # Freeze gzip timestamp only in this offline fixture; production remains unchanged.
    monkeypatch.setattr(gzip.time, "time", lambda: 1720000000)
    content = '{"名称":"真实脱敏格式","asOf":"2026-08-13"}'.encode()
    manifest = _manifest() | {"schemaVersion": schema}
    declared = manifest["artifacts"]["overview"]
    before = copy.deepcopy(manifest)
    expected = {"version": 1, "activationMode": mode, "manifest": manifest,
        "artifact": {"name": "overview", "key": declared["key"],
            "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
            "contentBase64": base64.b64encode(gzip.compress(content, compresslevel=6)).decode(),
            "contentEncoding": "gzip"}, "finalize": finalize}
    assert script.build_upload_body is build_upload_body
    assert build_upload_body(manifest, "overview", declared, content, finalize=finalize,
                             activation_mode=mode) == expected
    assert manifest == before
    assert gzip.decompress(base64.b64decode(expected["artifact"]["contentBase64"])) == content


@pytest.mark.parametrize("slot", ["dashboard", "etf_penetration", "full"])
@pytest.mark.parametrize("schema", [3, 4])
def test_script_capability_wrapper_matches_pure_contract(monkeypatch, slot, schema):
    payload = capabilities()
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    monkeypatch.setattr(script.requests, "get", lambda *a, **kw: (calls.append(kw), Response())[1])
    assert validate_publication_capabilities(payload, slot=slot, required_manifest_schema=schema) is None
    metrics = {}
    script.check_capabilities("https://fixture.invalid", {"Content-Type": "application/json", "Other": "x"},
                              slot=slot, required_manifest_schema=schema, metrics=metrics)
    assert metrics == {"workerRequests": 1}
    assert calls == [{"headers": {"Other": "x"}, "timeout": (10, 20), "allow_redirects": False}]


@pytest.mark.parametrize("field", ["readModelManifestSchemas", "schemaVersions", "pressureSchemaVersions", "stockShardCounts", "features"])
def test_missing_capability_rejected_by_both_entries(monkeypatch, field):
    payload = capabilities()
    del payload[field]
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    monkeypatch.setattr(script.requests, "get", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError) as pure:
        validate_publication_capabilities(payload)
    with pytest.raises(RuntimeError) as wrapper:
        script.check_capabilities("https://fixture.invalid", {})
    assert str(pure.value) == str(wrapper.value)


def test_pure_contract_does_not_use_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure publication contract attempted HTTP")
    monkeypatch.setattr(script.requests, "get", forbidden)
    monkeypatch.setattr(script.requests, "post", forbidden)
    validate_publication_capabilities(capabilities())
    build_upload_body(_manifest(), "overview", _manifest()["artifacts"]["overview"], b"{}", finalize=False)


@pytest.mark.parametrize("stage_only,schema", [(False, 3), (True, 3), (True, 4)])
def test_existing_cli_uses_same_payload_and_result_contract(tmp_path, monkeypatch, stage_only, schema):
    content = b'{"asOf":"2026-08-13"}'
    manifest = _manifest() | {"schemaVersion": schema, "slot": "dashboard"}
    manifest["artifacts"]["overview"].update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "overview.json").write_bytes(content)
    result_path = tmp_path / "result.json"
    metrics_path = tmp_path / "metrics.json"
    argv = ["publish_read_models.py", "--read-model-dir", str(tmp_path), "--result-output", str(result_path),
            "--metrics-output", str(metrics_path)] + (["--stage-only"] if stage_only else [])
    monkeypatch.setattr("sys.argv", argv)
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-not-a-secret")
    monkeypatch.setattr(script, "check_capabilities", lambda *a, **kw: None)
    calls = []
    response = {"status": "staged_candidate" if stage_only else "activated"}
    monkeypatch.setattr(script, "post", lambda url, body, headers, **kw: (calls.append(body), response)[1])
    script.main()
    assert len(calls) == 1
    assert calls[0]["activationMode"] == ("stage_only" if stage_only else "active")
    assert calls[0]["finalize"] is True
    assert calls[0]["manifest"] == manifest
    assert json.loads(result_path.read_text()) == response
    assert json.loads(metrics_path.read_text())["r2Bytes"] == len(content)


def test_invalid_slot_rejected_before_http(monkeypatch):
    monkeypatch.setattr(script.requests, "get", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    with pytest.raises(ValueError):
        script.check_capabilities("https://fixture.invalid", {}, slot="unknown")
    with pytest.raises(ValueError):
        validate_publication_capabilities(capabilities(), slot="unknown")
