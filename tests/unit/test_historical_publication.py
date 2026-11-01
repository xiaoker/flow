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
def server(tmp_path, *, observed="2026-08-05T09:00:00+08:00", extra=None, race=False):
    base = _snapshot()
    _, baseline = build_market_read_models(base, require_materialized=True)
    before = tmp_path / "active.json"; before.write_text(json.dumps(baseline))
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
