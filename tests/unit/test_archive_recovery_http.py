"""Archive-only inputs through actual Worker HTTP/SQLite, never production."""
import base64
import json
import os
from pathlib import Path
import subprocess

import requests
import pytest

from scripts.run_live_recovery import run_live_recovery
from trading_desk.recovery_execution import build_recovery_plan
from trading_desk.read_models import build_market_read_models
from test_phase0_archive import fixture_recipe, DATES
from test_pipeline_verify_adapter import setup

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("connect_closed", [False, True])
def test_archive_recovery_against_real_worker(tmp_path, monkeypatch, read_only, connect_closed):
    recipe, bodies = fixture_recipe(closed_days=[DATES[-1]] if connect_closed else [])
    refs = {ref["objectKey"]:ref for ref in recipe["files"]}
    seed = tmp_path/"archive-seed.json"
    seed.write_text(json.dumps([{"key":key,"sha256":refs[key]["sha256"],"base64":base64.b64encode(body).decode()}
        for key,body in bodies.items()]))
    key = f"daily-market-data:{DATES[-1]}:reconcile:v1"
    (tmp_path/"existing-candidate").mkdir()
    adapter, _ = setup(tmp_path/"existing-candidate")
    existing_path = adapter.source_dir/"manifest.json"
    existing = json.loads(existing_path.read_bytes())
    env = {**os.environ,"FLOW_FULL_FIXTURE_RUN_KEY":key,"FLOW_FULL_FIXTURE_TARGET":DATES[-1],
        "FLOW_FULL_FIXTURE_OWNER":"fixture-owner","FLOW_FULL_FIXTURE_REQUEST_LIMIT":"300",
        "FLOW_FULL_FIXTURE_HISTORY_INPUTS":str(seed),"FLOW_FULL_FIXTURE_ACTIVE_MANIFEST":str(existing_path)}
    process = subprocess.Popen(["node","--experimental-strip-types",str(ROOT/"web/tests/fixtures/full-pipeline-server.mjs")],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    try:
        port = process.stdout.readline().strip()
        assert port.isdigit(), process.stderr.read() if process.poll() is not None else "fixture did not start"
        origin = f"http://127.0.0.1:{port}"
        for name,value in {"FLOW_INGEST_API_KEY":"fixture-only","FLOW_CF_ACCESS_CLIENT_ID":"fixture-id",
                "FLOW_CF_ACCESS_CLIENT_SECRET":"fixture-secret"}.items():
            monkeypatch.setenv(name,value)
        monkeypatch.setattr("scripts.run_live_recovery.time.sleep",lambda seconds:None)
        plan = build_recovery_plan(campaign_id="fixture",recovery_id="fixture-archive",run_key=key,
            data_dates=[DATES[-1]],required_stages=["dashboard","publish"],artifact_root=".recovery/archive",
            state_dir=".recovery/archive/state",max_dates=1,max_requests=300,deadline="2026-12-31T00:00:00.000Z",
            budget_manifest={"archivePhase0Source":recipe})
        result = run_live_recovery(plan=plan,expected_plan_hash=plan["planHash"],attempt=0,
            producer_build_sha="a"*64,workspace_root=tmp_path,base_url=origin,
            preflight_only=read_only,allow_loopback_fixture=True)
        stats = requests.get(origin+"/fixture/stats",headers={"Authorization":"Bearer fixture-only"},timeout=10).json()
        assert stats["active"] == 1
        assert stats["activeIdentity"] == {"release_id":existing["releaseId"],"as_of":existing["asOf"],
            "manifest_sha256":existing["manifestSha256"]}
        if read_only:
            assert result["status"] == "LOCAL_SOURCE_CHECKED" and result["liveExecutionAllowed"] is False
            assert result["sourceQaCounts"]["fail"] == 0
            assert stats["r2Put"] == 0 and stats["sql"] == 0
            assert set(stats["http"]) == {"/api/v1/admin/history-artifact","/fixture/stats"}
            assert stats["r2Get"] == result["requestCount"]
            assert result["providerUsage"] == "UNKNOWN"
        else:
            assert result["status"] == "READY" and result["targetDate"] == DATES[-1]
            assert stats["r2Put"] > 0 and stats["sql"] > 0
            assert "/api/v1/admin/history-import" in stats["http"]
            assert "/api/v2/market-history" in stats["http"]
            assert not any("activate" in path or "claim" in path for path in stats["http"])
            # The real publisher passes the downloaded active manifest, which
            # can belong to a later day than this historical recovery. Source
            # preflight alone never exercises this production comparison.
            source = tmp_path / ".recovery/archive/source-bundle"
            snapshot = json.loads((source / "dashboard-snapshot.json").read_bytes())
            previous = json.loads((source / "previous-release.json").read_bytes())
            baseline = previous.get("manifest", previous)
            assert baseline["asOf"] > snapshot["asOf"]
            history_path = source / "market-history.json"
            history = json.loads(history_path.read_bytes()) if history_path.is_file() else None
            _, candidate = build_market_read_models(
                snapshot, require_materialized=True, market_history=history,
                previous_manifest=previous,
            )
            assert candidate["asOf"] == DATES[-1]
            assert candidate["slot"] == "dashboard"
            assert candidate["coverage"]["etf"]["tradingDays"] < baseline["coverage"]["etf"]["tradingDays"]
            after = requests.get(origin+"/fixture/stats",headers={"Authorization":"Bearer fixture-only"},timeout=10).json()
            assert after["activeIdentity"] == stats["activeIdentity"]
    finally:
        process.terminate()
        try: process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.communicate(timeout=10)
