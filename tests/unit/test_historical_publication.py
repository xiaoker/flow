"""Historical publication through actual built Worker, SQLite and normal reads."""
import json
import hashlib
import io
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from scripts.publish_read_models import build_upload_body
from scripts.activate_read_models import activate, verify_published_date
from trading_desk.etf_flow_state import materialize_dashboard_snapshot
from trading_desk.read_models import build_market_read_models, canonical_json_bytes
from test_read_models import _snapshot, _state

ROOT = Path(__file__).parents[2]
AUTH = {"Authorization": "Bearer fixture-only", "CF-Access-Client-Id": "fixture-id", "CF-Access-Client-Secret": "fixture-secret"}


@contextmanager
def server(tmp_path, *, observed="2026-08-05T09:00:00+08:00", extra=None, race=False, checkpoint_active=False):
    base = _snapshot()
    rolling_input = None
    if checkpoint_active:
        from test_etf_rolling_anchor import rolling_production_fixture
        base, rolling_input = rolling_production_fixture()
    _, baseline = build_market_read_models(base, require_materialized=True,
        producer_build_sha="a" * 40 if checkpoint_active else None,
        rolling_publication_input=rolling_input)
    before = tmp_path / "active.json"; before.write_bytes(canonical_json_bytes(baseline))
    empty = tmp_path / "seed.json"; empty.write_text("[]")
    snapshot = materialize_dashboard_snapshot(base, _state(), as_of="2026-08-05")
    snapshot["observedAt"] = observed
    payloads, manifest = build_market_read_models(snapshot, require_materialized=True)
    env = {**os.environ, "FLOW_FULL_FIXTURE_RUN_KEY": "daily-market-data:2026-08-05:reconcile:v1",
        "FLOW_FULL_FIXTURE_TARGET": "2026-08-05", "FLOW_FULL_FIXTURE_OWNER": "fixture-owner",
        "FLOW_FULL_FIXTURE_REQUEST_LIMIT": "150", "FLOW_FULL_FIXTURE_HISTORY_INPUTS": str(empty),
        "FLOW_FULL_FIXTURE_ACTIVE_MANIFEST": str(before)}
    if extra:
        path = tmp_path / "extra.json"; path.write_text(json.dumps(extra))
        env["FLOW_FULL_FIXTURE_EXTRA_RELEASE"] = str(path)
    if race: env["FLOW_FULL_FIXTURE_HISTORY_POINTER_RACE"] = "1"
    if checkpoint_active:
        env["FLOW_FULL_FIXTURE_RUN_KEY"] = f"daily-market-data:{baseline['asOf']}:reconcile:v1"
        env["FLOW_FULL_FIXTURE_TARGET"] = baseline["asOf"]
    process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "web/tests/fixtures/full-pipeline-server.mjs")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), process.stderr.read() if process.poll() is not None else "fixture startup failed"
        origin = f"http://127.0.0.1:{port}"
        for index, (name, descriptor) in enumerate(manifest["artifacts"].items()):
            body = build_upload_body(manifest, name, descriptor, canonical_json_bytes(payloads[name]),
                finalize=index == len(manifest["artifacts"]) - 1, activation_mode="stage_only")
            response = requests.post(origin + "/api/v1/admin/read-model-import", json=body, headers=AUTH, timeout=10)
            assert response.status_code == 200, response.text
        yield origin, baseline, manifest
    finally:
        process.terminate()
        try: process.communicate(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.communicate(timeout=10)


def test_explicit_historical_publication_is_normal_readable_without_latest_rollback(tmp_path, monkeypatch):
    for name, value in [("FLOW_INGEST_API_KEY", "fixture-only"), ("FLOW_CF_ACCESS_CLIENT_ID", "fixture-id"), ("FLOW_CF_ACCESS_CLIENT_SECRET", "fixture-secret")]:
        monkeypatch.setenv(name, value)
    with server(tmp_path) as (origin, baseline, manifest):
        request = {"releaseId": manifest["releaseId"], "asOf": manifest["asOf"], "slot": "dashboard"}
        legacy = requests.post(origin + "/api/v1/admin/read-model-activate", json=request, headers=AUTH, timeout=10)
        assert legacy.status_code == 200 and legacy.json()["status"].startswith("staged_")
        before = requests.get(origin + "/api/v2/views/radar", params={"date": manifest["asOf"], "slot": "dashboard"}, headers=AUTH, timeout=10)
        assert before.status_code != 200
        published = requests.post(origin + "/api/v1/admin/read-model-activate", json=request | {"publishHistory": True}, headers=AUTH, timeout=10)
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published_historical"
        proof = verify_published_date(origin, manifest=manifest, active_release_id=baseline["releaseId"])
        assert proof["artifactHashes"] == {key: value["sha256"] for key, value in manifest["artifacts"].items()}
        assert proof["requests"] == len(manifest["artifacts"]) + 2
        for name in manifest["artifacts"]:
            response = requests.get(origin + f"/api/v2/views/{name}", params={"date": manifest["asOf"], "slot": "dashboard"}, headers=AUTH, timeout=10)
            assert response.status_code == 200, response.text
            assert response.headers["x-flow-release-id"] == manifest["releaseId"]
        latest = requests.get(origin + "/api/v2/releases/latest", headers=AUTH, timeout=10).json()
        assert latest["releaseId"] == baseline["releaseId"] and latest["asOf"] == baseline["asOf"]
        stats = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        replay = requests.post(origin + "/api/v1/admin/read-model-activate", json=request | {"publishHistory": True}, headers=AUTH, timeout=10)
        assert replay.status_code == 200 and replay.json()["status"] == "published_historical"
        again = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert again["sqlChanges"] == stats["sqlChanges"]
        assert again["r2Put"] == stats["r2Put"]
        assert again["activeIdentity"] == stats["activeIdentity"]


def test_rolling_checkpoint_rejects_invalid_binding_without_storage_writes(tmp_path):
    with server(tmp_path) as (origin, baseline, _manifest):
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        endpoint = origin + "/api/v1/admin/etf-state"
        assert requests.post(endpoint, json={}, timeout=10).status_code == 401
        invalid = requests.post(endpoint, json={}, headers=AUTH, timeout=10)
        assert invalid.status_code == 422, invalid.text
        stale = requests.post(endpoint, json={
            "releaseId": "release:2026-08-01:0000000000000000",
            "manifestSha256": "0" * 64, "sha256": "0" * 64,
            "contentBase64": "",
        }, headers=AUTH, timeout=10)
        assert stale.status_code == 409, stale.text
        missing = requests.get(endpoint, params={"kind": "rolling-checkpoint"}, headers=AUTH, timeout=10)
        assert missing.status_code == 404, missing.text
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["sqlChanges"] == before["sqlChanges"]
        assert after["r2Put"] == before["r2Put"]
        assert after["activeIdentity"] == before["activeIdentity"]
        assert after["activeIdentity"]["release_id"] == baseline["releaseId"]


def test_rolling_checkpoint_binds_exact_active_release_and_is_immutable(tmp_path):
    import base64
    from scripts.bind_rolling_checkpoint import bind_checkpoint
    from trading_desk.bounded_http import BoundedHTTPExchange
    from trading_desk.etf_rolling_refs import unpack_rolling_checkpoint
    from test_etf_rolling_anchor import rolling_production_fixture
    with server(tmp_path, checkpoint_active=True) as (origin, baseline, _manifest):
        endpoint = origin + "/api/v1/admin/etf-state"
        wire = rolling_production_fixture()[1]["checkpoint"]
        _, refs, cache = unpack_rolling_checkpoint(wire, hashlib.sha256(wire).hexdigest())
        body = {"releaseId": baseline["releaseId"], "manifestSha256": baseline["manifestSha256"],
            "sha256": hashlib.sha256(wire).hexdigest(), "contentBase64": base64.b64encode(wire).decode(),
            "pipelineExecution": {"runKey": f"daily-market-data:{baseline['asOf']}:reconcile:v1",
                "leaseOwner": "fixture-owner", "attemptCount": 1,
                "targetDate": baseline["asOf"], "producerBuildSha": "a" * 40}}
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        wrong_digest = requests.post(endpoint, json={**body, "sha256": "f" * 64}, headers=AUTH, timeout=10)
        assert wrong_digest.status_code == 409, wrong_digest.text
        wrong_owner = requests.post(endpoint, json={**body, "pipelineExecution": {
            **body["pipelineExecution"], "leaseOwner": "old-owner"}}, headers=AUTH, timeout=10)
        assert wrong_owner.status_code == 409, wrong_owner.text
        rejected = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert rejected["r2Put"] == before["r2Put"]
        result = bind_checkpoint(manifest=baseline, activation={"status": "activated",
            "slot": "dashboard", "asOf": baseline["asOf"], "releaseId": baseline["releaseId"]},
            wire=wire, execution=body["pipelineExecution"],
            exchange=BoundedHTTPExchange(requests.Session()), headers=AUTH, url=endpoint)
        assert result["status"] == "BOUND"
        assert result["workerRequests"] == 1 and result["d1RowsRead"] is None
        readback = requests.get(endpoint, params={"kind": "rolling-checkpoint"}, headers=AUTH, timeout=10)
        assert readback.status_code == 200, readback.text
        assert readback.content == wire
        assert readback.headers["x-flow-checkpoint-sha256"] == body["sha256"]
        assert unpack_rolling_checkpoint(readback.content, body["sha256"])[1:] == (refs, cache)
        first = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        replay = requests.post(endpoint, json=body, headers=AUTH, timeout=10)
        assert replay.status_code == 200, replay.text
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert first["r2Put"] == before["r2Put"] + 2
        assert after["r2Put"] == first["r2Put"]
        assert after["sqlChanges"] == before["sqlChanges"]
        assert after["activeIdentity"] == before["activeIdentity"]


def test_native_rolling_publication_is_readable_without_activating_v4_or_rolling_back_latest(tmp_path):
    from test_etf_rolling_anchor import rolling_production_fixture
    base, inputs = rolling_production_fixture()
    payloads, manifest = build_market_read_models(base, require_materialized=True,
        producer_build_sha="a" * 40, rolling_publication_input=inputs)
    with server(tmp_path) as (origin, baseline, _candidate):
        for index, (name, descriptor) in enumerate(manifest["artifacts"].items()):
            response = requests.post(origin + "/api/v1/admin/read-model-import", json=build_upload_body(
                manifest, name, descriptor, canonical_json_bytes(payloads[name]),
                finalize=index == len(manifest["artifacts"]) - 1, activation_mode="stage_only"),
                headers=AUTH, timeout=10)
            assert response.status_code == 200, response.text
        published = requests.post(origin + "/api/v1/admin/read-model-activate", json={
            "releaseId": manifest["releaseId"], "asOf": manifest["asOf"],
            "slot": "dashboard", "publishHistory": True}, headers=AUTH, timeout=10)
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published_historical"
        radar = requests.get(origin + "/api/v2/views/radar", params={
            "date": manifest["asOf"], "slot": "dashboard"}, headers=AUTH, timeout=10)
        assert radar.status_code == 200, radar.text
        for row in radar.json()["data"]["etfTerminal"]["instruments"]:
            assert row["periods"]["2w"] == 10 and row["periods"]["1m"] == 21
            assert row["estimatedCashPeriods"].get("1m") is None
        latest = requests.get(origin + "/api/v2/releases/latest", headers=AUTH, timeout=10).json()
        assert latest["releaseId"] == baseline["releaseId"]


def test_missing_rolling_proof_does_not_block_new_confirmed_daily_release(tmp_path):
    base = _snapshot()
    base["quality"]["warn"] = 0.0  # Preserve producer float spelling across JSON/Worker storage.
    base["publication"] = {"status": "confirmed"}
    base["observedAt"] = "2026-08-06T10:00:00+08:00"
    payloads, manifest = build_market_read_models(base, require_materialized=True,
        producer_build_sha="a" * 64, rolling_unavailable_reason="rolling_proof_unavailable")
    with server(tmp_path) as (origin, baseline, _candidate):
        for index, (name, descriptor) in enumerate(manifest["artifacts"].items()):
            uploaded = requests.post(origin + "/api/v1/admin/read-model-import", json=build_upload_body(
                manifest, name, descriptor, canonical_json_bytes(payloads[name]),
                finalize=index == len(manifest["artifacts"]) - 1, activation_mode="stage_only"), headers=AUTH, timeout=10)
            assert uploaded.status_code == 200, uploaded.text
        result = requests.post(origin + "/api/v1/admin/read-model-activate", json={
            "releaseId": manifest["releaseId"], "asOf": manifest["asOf"], "slot": "dashboard"}, headers=AUTH, timeout=10)
        assert result.status_code == 200 and result.json()["status"] == "activated", result.text
        latest = requests.get(origin + "/api/v2/releases/latest", headers=AUTH, timeout=10).json()
        assert latest["releaseId"] == manifest["releaseId"] != baseline["releaseId"]
        radar = requests.get(origin + "/api/v2/views/radar", headers=AUTH, timeout=10)
        assert radar.status_code == 200, radar.text
        rows = radar.json()["data"]["etfTerminal"]["instruments"]
        for row in rows:
            assert row["periods"]["1d"] is not None
            assert row["periods"]["2w"] is None and row["periods"]["1m"] is None
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        checkpoint = requests.get(origin + "/api/v1/admin/etf-state", params={"kind": "rolling-checkpoint"}, headers=AUTH, timeout=10)
        assert checkpoint.status_code == 404
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["r2Get"] == before["r2Get"]


@pytest.mark.parametrize("fault", ["missing", "wire", "semantic"])
def test_native_production_manifest_bytes_reject_mismatch_before_any_write(tmp_path, fault):
    from test_etf_rolling_anchor import rolling_production_fixture
    base, inputs = rolling_production_fixture()
    payloads, manifest = build_market_read_models(base, require_materialized=True,
        producer_build_sha="a" * 64, rolling_publication_input=inputs)
    name = "bootstrap"
    body = build_upload_body(manifest, name, manifest["artifacts"][name], canonical_json_bytes(payloads[name]),
        finalize=False, activation_mode="stage_only")
    if fault == "missing":
        del body["productionManifestJson"]
    elif fault == "wire":
        body["productionManifestJson"] += " "
    else:
        body["manifest"] = {**manifest, "quality": {**manifest["quality"], "warn": 123}}
    with server(tmp_path) as (origin, _baseline, _candidate):
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        reply = requests.post(origin + "/api/v1/admin/read-model-import", json=body, headers=AUTH, timeout=10)
        assert reply.status_code == (422 if fault == "missing" else 409), reply.text
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["r2Put"] == before["r2Put"] and after["sqlChanges"] == before["sqlChanges"]


@pytest.mark.parametrize("fault", ["auth", "slot", "date", "intent"])
def test_historical_intent_keeps_existing_guards(tmp_path, fault):
    with server(tmp_path) as (origin, _baseline, manifest):
        body = {"releaseId": manifest["releaseId"], "asOf": manifest["asOf"], "slot": "dashboard", "publishHistory": True}
        headers = dict(AUTH)
        if fault == "auth": headers["Authorization"] = "Bearer wrong"
        if fault == "slot": body["slot"] = "etf_penetration"
        if fault == "date": body["asOf"] = "2026-08-04"
        if fault == "intent": body["publishHistory"] = "true"
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        result = requests.post(origin + "/api/v1/admin/read-model-activate", json=body, headers=headers, timeout=10)
        assert result.status_code in {401, 422}
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["sqlChanges"] == before["sqlChanges"]
        assert after["activeIdentity"] == before["activeIdentity"]


def test_v4_isolation_cannot_be_bypassed_by_historical_intent(tmp_path):
    from test_pipeline_verify_adapter import setup
    folder = tmp_path / "v4"; folder.mkdir()
    adapter, _args = setup(folder)
    manifest = json.loads((adapter.source_dir / "manifest.json").read_bytes())
    assert manifest["schemaVersion"] == 4
    with server(tmp_path, extra={"manifest": manifest, "status": "STAGED"}) as (origin, baseline, _candidate):
        result = requests.post(origin + "/api/v1/admin/read-model-activate", json={"releaseId": manifest["releaseId"],
            "asOf": manifest["asOf"], "slot": "dashboard", "publishHistory": True}, headers=AUTH, timeout=10)
        assert result.status_code == 409 and result.json()["error"] == "candidate_manifest_not_activatable"
        assert requests.get(origin + "/api/v2/releases/latest", headers=AUTH, timeout=10).json()["releaseId"] == baseline["releaseId"]


def test_older_revision_cannot_replace_newer_published_history(tmp_path):
    snapshot = materialize_dashboard_snapshot(_snapshot(), _state(), as_of="2026-08-05")
    snapshot["observedAt"] = "2026-08-05T10:00:00+08:00"
    _, newer = build_market_read_models(snapshot, require_materialized=True)
    with server(tmp_path, extra={"manifest": newer, "status": "SUPERSEDED"}) as (origin, _baseline, manifest):
        before = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        result = requests.post(origin + "/api/v1/admin/read-model-activate", json={"releaseId": manifest["releaseId"],
            "asOf": manifest["asOf"], "slot": "dashboard", "publishHistory": True}, headers=AUTH, timeout=10)
        assert result.status_code == 409 and result.json()["error"] == "historical_publication_conflict"
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["sqlChanges"] == before["sqlChanges"]
        assert after["activeIdentity"] == before["activeIdentity"]


def test_concurrent_latest_change_fences_history_publication(tmp_path):
    snapshot = _snapshot(); snapshot["observedAt"] = "2026-08-06T10:00:00+08:00"
    _, concurrent = build_market_read_models(snapshot, require_materialized=True)
    with server(tmp_path, extra={"manifest": concurrent, "status": "SUPERSEDED"}, race=True) as (origin, _baseline, manifest):
        result = requests.post(origin + "/api/v1/admin/read-model-activate", json={"releaseId": manifest["releaseId"],
            "asOf": manifest["asOf"], "slot": "dashboard", "publishHistory": True}, headers=AUTH, timeout=10)
        assert result.status_code == 409 and result.json()["error"] == "historical_publication_conflict"
        after = requests.get(origin + "/fixture/stats", headers=AUTH, timeout=10).json()
        assert after["activeIdentity"]["release_id"] == concurrent["releaseId"]
        assert sum(row["status"] == "STAGED" for row in after["releases"]) == 1
        normal = requests.get(origin + "/api/v2/views/radar", params={"date": manifest["asOf"], "slot": "dashboard"}, headers=AUTH, timeout=10)
        assert normal.status_code != 200


@pytest.mark.parametrize("fault", [None, "hash", "date", "release", "latest", "oversize"])
def test_normal_readback_checks_exact_bytes_and_identity_without_admin_access(monkeypatch, fault):
    wire = b'{"asOf":"2026-08-05"}'
    manifest = {"releaseId": "release:2026-08-05:0123456789abcdef", "asOf": "2026-08-05", "slot": "dashboard",
        "artifacts": {"overview": {"bytes": len(wire), "sha256": hashlib.sha256(wire).hexdigest()}}}
    active = "release:2026-08-06:1111111111111111"
    calls = []
    class Raw(io.BytesIO):
        def read(self, size=-1, decode_content=False):
            return super().read(size)
    class Reply:
        def __init__(self, body, release, date):
            self.raw = Raw(body)
            self.headers = {"x-flow-release-id": release, "x-flow-as-of": date}
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def raise_for_status(self): pass
    def get(url, **kwargs):
        calls.append(url)
        assert "/admin/" not in url and "Authorization" not in kwargs["headers"]
        if url.endswith("/latest"):
            chosen = "changed" if fault == "latest" and len(calls) > 1 else active
            return Reply(json.dumps({"releaseId": chosen, "asOf": "2026-08-06"}).encode(), chosen, "2026-08-06")
        assert kwargs["params"] == {"date": "2026-08-05", "slot": "dashboard"}
        body = b'x' * (len(wire) + 1) if fault == "oversize" else wire.replace(b"05", b"04") if fault == "hash" else wire
        return Reply(body, "wrong" if fault == "release" else manifest["releaseId"], "2026-08-04" if fault == "date" else "2026-08-05")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "fixture-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setattr("scripts.activate_read_models.requests.get", get)
    if fault:
        with pytest.raises(RuntimeError, match="published_date_"):
            verify_published_date("https://fixture.invalid", manifest=manifest, active_release_id=active)
    else:
        proof = verify_published_date("https://fixture.invalid", manifest=manifest, active_release_id=active)
        assert proof["requests"] == 3 and proof["artifactHashes"]["overview"] == manifest["artifacts"]["overview"]["sha256"]


def test_invalid_local_history_manifest_is_rejected_before_mutation(tmp_path, monkeypatch):
    from scripts import activate_read_models
    manifest = {"releaseId": "release:2026-08-05:0123456789abcdef", "asOf": "2026-08-05", "slot": "dashboard",
        "artifacts": {"overview": {"bytes": 5_000_001, "sha256": "a" * 64}}}
    path = tmp_path / "manifest.json"; path.write_text(json.dumps(manifest))
    monkeypatch.setattr(sys, "argv", ["activate", "--release-id", manifest["releaseId"], "--as-of", manifest["asOf"],
        "--publish-history", "--read-model-manifest", str(path), "--readback-output", str(tmp_path / "proof.json")])
    def forbidden(*_args, **_kwargs): raise AssertionError("must reject before POST")
    monkeypatch.setattr(activate_read_models.requests, "post", forbidden)
    with pytest.raises(ValueError, match="published_date_artifact_size_invalid"):
        activate_read_models.main()
