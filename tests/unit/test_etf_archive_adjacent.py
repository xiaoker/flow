import hashlib
from pathlib import Path
import pandas as pd
import pytest
from trading_desk.etf_archive_adjacent import archived_inputs, load_archived_frame
from trading_desk.etf_facts import build_canonical_etf_daily_fact
from trading_desk.etf_daily_changes import build_official_etf_daily_changes

def reference(tmp_path):
    data = b"trade_date,market,instrument_code,instrument_name,shares\n2026-10-12,SZSE,158000,ETF,39925835\n"
    (tmp_path / "previous.csv").write_bytes(data)
    return {"sourceId":"szse", "tradeDate":"2026-10-12", "objectKey":"market-data/normalized/szse/etf_scale/2026-10-12/" + "a"*64 + "/data.csv",
        "sha256":hashlib.sha256(data).hexdigest(), "revisionId":"a"*64, "normalizedPath":"previous.csv"}

@pytest.mark.parametrize("pointer_identity", ["digest", "legacy_release"])
def test_archived_previous_restores_exact_daily_change(tmp_path, pointer_identity):
    ref = reference(tmp_path)
    if pointer_identity == "legacy_release":
        ref["revisionId"] = "etf_scale:szse:2026-10-12:" + "a" * 16
    manifest = {"requested_dates":["2026-10-09","2026-10-12","2026-10-13"], "archivedEtfInputs":[ref]}
    older = archived_inputs(manifest, tmp_path, "2026-10-13")[0][1]
    current = older.copy(); current["trade_date"] = "2026-10-13"; current["shares"] = 38925835
    def changes(frame):
        canonical = build_canonical_etf_daily_fact(pd.DataFrame(), frame, manifest["requested_dates"])
        return build_official_etf_daily_changes(canonical, as_of="2026-10-13", source_release_ids=["etf_scale:szse:2026-10-13:"+"b"*16])
    before = changes(current)
    assert before["marketStatus"][1]["status"] == "missing_previous"
    assert before["rows"][0]["changeShares"] is None
    after = changes(pd.concat([older,current],ignore_index=True))
    assert after["rows"][0]["changeShares"] == -1000000
    assert after["rows"][0]["previousTradeDate"] == "2026-10-12"
    assert after["rows"][0]["aligned"] is True
    from trading_desk.history_bundle import _build_official_etf_daily_changes_from_artifacts
    from trading_desk.dashboard import _build_etf_terminal_data
    current.to_csv(tmp_path / "current.csv", index=False)
    artifact={"source":{"source_id":"szse","dataset":"etf_scale"},"requested_date":"2026-10-13",
        "normalized_path":"current.csv","sha256":"b"*64}
    serialized=[{"sourceId":"szse","dataset":"etf_scale","requestedDate":"2026-10-13",
        "sha256":"b"*64,"releaseId":"etf_scale:szse:2026-10-13:"+"b"*16}]
    contract=_build_official_etf_daily_changes_from_artifacts(artifacts=[artifact],serialized=serialized,
        as_of="2026-10-13",workspace_root=tmp_path,archived=[(ref,older)])
    assert contract["rows"][0]["changeShares"] == -1000000
    assert "etf_scale:szse:2026-10-12:"+"a"*16 in contract["sourceReleaseIds"]
    terminal=_build_etf_terminal_data(pd.DataFrame(),pd.concat([older,current],ignore_index=True),manifest["requested_dates"])
    assert terminal["instruments"][0]["periods"]["1d"] == -0.01

@pytest.mark.parametrize("fault", ["hash","date","market","duplicate","path","count","legacy_date","legacy_source","legacy_digest","revision_digest"])
def test_invalid_archived_reference_is_rejected(tmp_path, fault):
    ref = reference(tmp_path)
    if fault == "hash": ref["sha256"]="0"*64
    if fault == "date": ref["tradeDate"]="2026-10-09"
    if fault == "path": ref["normalizedPath"]="../previous.csv"
    if fault == "legacy_date": ref["revisionId"]="etf_scale:szse:2026-10-09:"+"a"*16
    if fault == "legacy_source": ref["revisionId"]="etf_scale:sse:2026-10-12:"+"a"*16
    if fault == "legacy_digest": ref["revisionId"]="etf_scale:szse:2026-10-12:"+"b"*16
    if fault == "revision_digest": ref["revisionId"]="b"*64
    if fault in {"market","duplicate"}:
        path = tmp_path / "previous.csv"
        data = path.read_bytes()
        data = data.replace(b"SZSE",b"SSE") if fault=="market" else data + data.splitlines(keepends=True)[1]
        path.write_bytes(data); ref["sha256"]=hashlib.sha256(data).hexdigest()
    manifest={"requested_dates":["2026-10-12","2026-10-13"],"archivedEtfInputs":[ref]*(2 if fault=="count" else 1)}
    with pytest.raises(ValueError): archived_inputs(manifest,tmp_path,"2026-10-13")

def test_default_manifest_does_not_read_archive(tmp_path):
    assert archived_inputs({"requested_dates":["2026-10-12","2026-10-13"]},tmp_path,"2026-10-13")==[]

@pytest.mark.parametrize("legacy", [False, True])
def test_derived_qa_uses_same_verified_baseline_without_mutating_collected_rows(tmp_path, legacy):
    from trading_desk.release_qa import _with_archived_etf_inputs, _canonical_etf_delta
    ref = reference(tmp_path)
    if legacy:
        ref["revisionId"] = "etf_scale:szse:2026-10-12:" + "a"*16
    current = pd.read_csv(tmp_path / "previous.csv", dtype={"instrument_code": str})
    current["trade_date"] = "2026-10-13"
    current["shares"] = current["shares"] - 1000000
    collected = {"szse_etf": current}
    manifest = {"requested_dates":["2026-10-12","2026-10-13"],"archivedEtfInputs":[ref]}
    derived = _with_archived_etf_inputs(manifest, collected, tmp_path, "2026-10-13")
    total, evidence = _canonical_etf_delta(derived, dates=manifest["requested_dates"], current_date="2026-10-13")
    assert total == pytest.approx(-0.01)
    assert evidence["comparable_count"] == 1
    assert len(collected["szse_etf"]) == 1 and len(derived["szse_etf"]) == 2
    ref["sha256"] = "0"*64
    with pytest.raises(ValueError, match="adjacent_archive_hash_mismatch"):
        _with_archived_etf_inputs(manifest, collected, tmp_path, "2026-10-13")

def test_workflow_hydrates_before_both_producers():
    root=Path(__file__).resolve().parents[2]
    text=(root/".github/workflows/market-data-collect.yml").read_text()
    assert text.index("python -m trading_desk.etf_archive_adjacent") < text.index("python scripts/build_dashboard_snapshot.py --output dashboard-snapshot-base.json") < text.index("python scripts/build_history_bundle.py --snapshot dashboard-snapshot-base.json")

def test_pending_source_does_not_hydrate_or_change_manifest(tmp_path, monkeypatch):
    import json
    import trading_desk.etf_archive_adjacent as adjacent
    reference(tmp_path)
    folder = tmp_path / "reports/phase0"
    folder.mkdir(parents=True)
    manifest = folder / "latest-manifest.json"
    payload = json.dumps({"requested_dates":["2026-10-12","2026-10-13"],"combined_paths":{"szse_etf":"previous.csv"}})
    manifest.write_text(payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adjacent.requests, "get", lambda *a, **k: pytest.fail("pending source must not request archive"))
    adjacent.main()
    assert manifest.read_text() == payload

@pytest.mark.parametrize("data", [b"", b"\n", b"trade_date,market,instrument_code,shares\n"])
def test_deferred_empty_current_source_does_not_abort_preparation(tmp_path, monkeypatch, capsys, data):
    import json
    import trading_desk.etf_archive_adjacent as adjacent
    (tmp_path/"current.csv").write_bytes(data)
    folder=tmp_path/"reports/phase0"; folder.mkdir(parents=True)
    manifest=folder/"latest-manifest.json"
    payload=json.dumps({"requested_dates":["2026-10-14","2026-10-15"],
        "combined_paths":{"szse_etf":"current.csv"}})
    manifest.write_text(payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adjacent.requests,"get",lambda *a,**k:pytest.fail("empty source must not fetch archive"))
    adjacent.main()
    assert "adjacent_archive_no_current" in capsys.readouterr().out
    assert manifest.read_text()==payload
    assert not (folder/"adjacent-szse.csv").exists()

@pytest.mark.parametrize("fault", ["missing", "malformed"])
def test_invalid_current_file_is_not_swallowed_as_deferred(tmp_path, monkeypatch, fault):
    import json
    import trading_desk.etf_archive_adjacent as adjacent
    if fault=="malformed":
        (tmp_path/"current.csv").write_text('trade_date,market\n"unterminated')
    folder=tmp_path/"reports/phase0"; folder.mkdir(parents=True)
    manifest=folder/"latest-manifest.json"
    payload=json.dumps({"requested_dates":["2026-10-14","2026-10-15"],"combined_paths":{"szse_etf":"current.csv"}})
    manifest.write_text(payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adjacent.requests,"get",lambda *a,**k:pytest.fail("invalid source must not fetch archive"))
    with pytest.raises(FileNotFoundError if fault=="missing" else pd.errors.ParserError):
        adjacent.main()
    assert manifest.read_text()==payload

def test_deferred_source_reaches_existing_dashboard_pending_gate(tmp_path, monkeypatch):
    import json
    import trading_desk.etf_archive_adjacent as adjacent
    from trading_desk.dashboard import build_dashboard_snapshot
    dates=["2026-10-14","2026-10-15"]
    (tmp_path/"current.csv").write_bytes(b"\n")
    pd.DataFrame([{"trade_date":d,"instrument_code":"510300","instrument_name":"沪深300ETF","shares":100}
        for d in dates]).to_csv(tmp_path/"sse.csv",index=False)
    pd.DataFrame([{"trade_date":d,"financing_balance_yuan":1000} for d in dates]).to_csv(tmp_path/"margin.csv",index=False)
    pd.DataFrame([{"trade_date":d,"market":m,"direction":"southbound","total_turnover":1000,"net_buy":100}
        for d in dates for m in ("SSE","SZSE")]).to_csv(tmp_path/"connect.csv",index=False)
    folder=tmp_path/"reports/phase0"; folder.mkdir(parents=True)
    manifest=folder/"latest-manifest.json"
    manifest.write_text(json.dumps({"requested_dates":dates,"finished_at":"2026-10-15T19:15:00+08:00",
        "combined_paths":{"szse_etf":"current.csv","sse_etf":"sse.csv","sse_margin":"margin.csv",
            "szse_margin":"margin.csv","stock_connect_summary":"connect.csv"}}))
    quality=folder/"quality.json"
    quality.write_text(json.dumps({"counts":{"pass":4,"warn":0,"fail":0},"phase0_gate_status":"pending"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adjacent.requests,"get",lambda *a,**k:pytest.fail("deferred source must not fetch"))
    adjacent.main()
    snapshot=build_dashboard_snapshot(manifest_path=manifest,quality_path=quality,workspace_root=tmp_path)
    assert snapshot["publication"]["status"]=="pending"
    assert snapshot["publication"]["pendingDomains"]
    assert snapshot["publication"]["targetAsOf"]==dates[-1]
    assert not json.loads(manifest.read_text()).get("archivedEtfInputs")

@pytest.mark.parametrize("status", [200,404,403,503])
@pytest.mark.parametrize("pointer_identity", ["digest", "legacy_release"])
def test_one_indexed_read_and_missing_vs_control_failure(tmp_path, monkeypatch, status, pointer_identity):
    import json
    import trading_desk.etf_archive_adjacent as adjacent
    ref=reference(tmp_path)
    if pointer_identity == "legacy_release":
        ref["revisionId"] = "etf_scale:szse:2026-10-12:" + "a" * 16
    data=(tmp_path/"previous.csv").read_bytes()
    current=pd.read_csv(tmp_path/"previous.csv",dtype={"instrument_code":str})
    current["trade_date"]="2026-10-13"
    current.to_csv(tmp_path/"current.csv",index=False)
    folder=tmp_path/"reports/phase0"; folder.mkdir(parents=True)
    manifest=folder/"latest-manifest.json"
    manifest.write_text(json.dumps({"requested_dates":["2026-10-12","2026-10-13"],"combined_paths":{"szse_etf":"current.csv"}}))
    class Response:
        status_code=status
        headers={"x-flow-normalized-key":ref["objectKey"],"x-flow-normalized-sha256":ref["sha256"],"x-flow-pcf-revision":ref["revisionId"]}
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def iter_content(self,*args): yield data
    calls=[]
    def get(url,**kwargs): calls.append((url,kwargs)); return Response()
    monkeypatch.setattr(adjacent.requests,"get",get)
    monkeypatch.chdir(tmp_path)
    for key in ["FLOW_INGEST_API_KEY","FLOW_CF_ACCESS_CLIENT_ID","FLOW_CF_ACCESS_CLIENT_SECRET"]:
        monkeypatch.setenv(key,"fixture-only")
    if status in {403,503}:
        with pytest.raises(ValueError,match="control_rejected"): adjacent.main()
    else: adjacent.main()
    assert len(calls)==1
    assert calls[0][0]=="https://flow.xiaoker.com/api/v1/admin/history-artifact"
    assert calls[0][1]["params"]["indexed"]=="1" and calls[0][1]["allow_redirects"] is False
    saved=json.loads(manifest.read_bytes())
    assert bool(saved.get("archivedEtfInputs")) == (status==200)
