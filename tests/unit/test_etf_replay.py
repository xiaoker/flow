import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from trading_desk.etf_period_dependencies import validate_official_calendar, OFFICIAL_MARGIN_DATES_SOURCE
from trading_desk.etf_replay import replay_snapshot, REPLAY_CONTRACT
from trading_desk.pipeline_application import canonical_sha256
from trading_desk.read_models import build_market_read_models, canonical_json_bytes, ReadModelError
from trading_desk.read_model_capabilities import CAPABILITY_CONTRACT_VERSION
from test_read_model_capabilities import _snapshot


def source():
    snapshot = _snapshot()
    dates = ["2026-08-03","2026-08-04","2026-08-05","2026-08-06"]
    calendar={"sourceId":"sse_official_trading_calendar","sha256":"a"*64,"dates":dates,"datesSha256":canonical_sha256(dates)}
    parts=[]
    for market,code,name in (("sse","510300","沪深300ETF"),("szse","159915","创业板ETF")):
        for i,day in enumerate(dates):
            data=f'trade_date,market,instrument_code,instrument_name,shares\n{day},{market.upper()},{code},{name},{100000000+i*1000000}\n'.encode()
            parts.append({"sourceId":market,"tradeDate":day,"objectKey":f'market-data/normalized/{market}/etf_scale/{day}/{"a"*64}/data.csv',
                "sha256":hashlib.sha256(data).hexdigest(),"contentBase64":base64.b64encode(data).decode()})
    snapshot["etfReplayInput"]={"contractVersion":REPLAY_CONTRACT,"calendar":calendar,"partitions":parts}
    return snapshot


def test_real_builder_consumes_replay_and_preserves_legacy_no_profile():
    value=source()
    result=replay_snapshot(value)
    assert "etfReplayInput" not in result
    assert result["publication"]["status"]=="confirmed"
    assert [r["periods"]["3d"] for r in result["etfTerminal"]["instruments"]]==[0.03,0.03]
    proof=result["etfTerminal"]["windowEvidence"]
    assert proof["periods"]["3d"]["expectedChanges"]==3
    payloads,manifest=build_market_read_models(value,require_materialized=True,capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    core=manifest["capabilityContract"]["capabilities"]["etf_daily_core"]
    assert core["status"]=="READY"
    assert manifest["capabilityContract"]["capabilities"]["etf_periods"]["periods"]["3d"]["shares"]["status"]=="READY"
    assert "contentBase64" not in json.dumps(payloads)
    reordered=deepcopy(value);reordered["etfReplayInput"]["partitions"].reverse()
    assert build_market_read_models(reordered,require_materialized=True,capability_contract_version=CAPABILITY_CONTRACT_VERSION)==(payloads,manifest)
    legacy=_snapshot()
    assert build_market_read_models(legacy)[1]["schemaVersion"]==3
    with pytest.raises(ReadModelError,match="candidate_contract_required"):
        build_market_read_models(value)


def test_cross_border_etf_identity_survives_canonical_replay():
    value = source()
    for part in value["etfReplayInput"]["partitions"]:
        if part["sourceId"] == "szse":
            data = base64.b64decode(part["contentBase64"]).replace(
                "创业板ETF".encode(), "港股通互联网ETF富国".encode()
            )
            part["contentBase64"] = base64.b64encode(data).decode()
            part["sha256"] = hashlib.sha256(data).hexdigest()
    result = replay_snapshot(value)
    rows = result["etfTerminal"]["instruments"]
    row = next(r for r in rows if r["market"] == "SZSE")
    assert row["assetClass"] == "跨境"
    assert row["classificationRule"] == "cross_border"
    assert row["periods"]["3d"] == 0.03


def test_missing_market_and_missing_day_are_not_promoted():
    value=source()
    value["etfReplayInput"]["partitions"]=[p for p in value["etfReplayInput"]["partitions"] if p["sourceId"]=="sse"]
    result=replay_snapshot(value)
    assert result["publication"]["status"]=="pending"
    _,manifest=build_market_read_models(value,require_materialized=True,capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    assert manifest["capabilityContract"]["capabilities"]["etf_daily_core"]["status"]!="READY"
    value=source()
    value["etfReplayInput"]["partitions"]=[p for p in value["etfReplayInput"]["partitions"] if p["tradeDate"]!="2026-08-04"]
    result=replay_snapshot(value)
    assert all(r["periods"]["3d"] is None for r in result["etfTerminal"]["instruments"])
    assert all(r["periods"]["1d"]==0.01 for r in result["etfTerminal"]["instruments"])
    assert result["publication"]["status"]=="confirmed"


@pytest.mark.parametrize("complete,zero", [(False,False),(True,False),(True,True)])
def test_group_amount_requires_complete_nav_samples_and_keeps_real_zero(complete,zero):
    from trading_desk.etf_flow_state import materialize_etf_terminal
    value = source()
    from test_read_model_capabilities import _state
    state = _state()
    state["officialCalendar"] = value["etfReplayInput"]["calendar"]
    # Two same-category ETFs, complete share facts; one missing NAV session.
    first = state["instruments"][0]
    first["values"] = [0.0]*4 if zero else [1.0]*4
    first["cashValues"] = [0.0]*4 if zero else [2.0]*4
    if not complete:
        first["cashValues"][2] = None
    second = deepcopy(first)
    second["code"] = "510500"
    second["cashValues"][2] = None
    state["instruments"] = [first,second]
    result = materialize_etf_terminal(state)
    assert result["groupPeriods"][0]["estimatedCashPeriods"]["3d"] == (0.0 if zero else 6.0 if complete else None)
    assert result["instruments"][1]["estimatedCashPeriods"]["3d"] is None


@pytest.mark.parametrize("fault",["hash","date","duplicate","key","version","negative_shares","base64","calendar_hash"])
def test_invalid_replay_rejected(fault):
    value=source();profile=value["etfReplayInput"];part=profile["partitions"][0]
    if fault=="hash": part["sha256"]="0"*64
    elif fault=="date": part["tradeDate"]="2026-08-07"
    elif fault=="duplicate": profile["partitions"].append(deepcopy(part))
    elif fault=="key": part["objectKey"]="outside/data.csv"
    elif fault=="version": profile["contractVersion"]="unknown"
    elif fault=="calendar_hash": profile["calendar"]["datesSha256"]="0"*64
    elif fault=="base64":part["contentBase64"]="%%%"
    else:
        data=base64.b64decode(part["contentBase64"]).replace(b'100000000',b'-1')
        part["sha256"]=hashlib.sha256(data).hexdigest();part["contentBase64"]=base64.b64encode(data).decode()
    with pytest.raises(ValueError):replay_snapshot(value)


def test_source_revision_invalidates_only_semantic_result_not_input():
    value=source();before=deepcopy(value)
    first=replay_snapshot(value)
    part=value["etfReplayInput"]["partitions"][-1]
    data=base64.b64decode(part["contentBase64"]).replace(b'103000000',b'104000000')
    part["sha256"]=hashlib.sha256(data).hexdigest();part["contentBase64"]=base64.b64encode(data).decode()
    second=replay_snapshot(value)
    assert first["etfTerminal"]["windowEvidence"]["inputSha256"]!=second["etfTerminal"]["windowEvidence"]["inputSha256"]
    assert first["etfTerminal"]["instruments"][0]["periods"]["3d"]==0.03
    assert before["etfReplayInput"]["partitions"][-1]["sha256"]!=part["sha256"]


def test_official_business_date_evidence_is_not_renamed_calendar():
    dates=["2026-08-03","2026-08-04"]
    response={"result":[{"opDate":d.replace('-','')} for d in dates],"pageHelp":{"pageCount":1,"total":2}}
    data=canonical_json_bytes(response)
    cal={"sourceId":OFFICIAL_MARGIN_DATES_SOURCE,"sha256":hashlib.sha256(data).hexdigest(),"dates":dates,"datesSha256":canonical_sha256(dates),
        "evidence":{"contentBase64":base64.b64encode(data).decode(),"rangeStart":dates[0],"rangeEnd":dates[-1]}}
    assert validate_official_calendar(cal)[0]==dates
    for field in ("pageCount","total"):
        broken=deepcopy(response);broken["pageHelp"][field]=3
        raw=canonical_json_bytes(broken);bad=deepcopy(cal)
        bad["sha256"]=hashlib.sha256(raw).hexdigest();bad["evidence"]["contentBase64"]=base64.b64encode(raw).decode()
        with pytest.raises(ValueError):validate_official_calendar(bad)
    bad=deepcopy(cal);bad.pop("evidence")
    with pytest.raises(ValueError):validate_official_calendar(bad)


def test_real_candidate_builder_and_verify_resume_use_same_saved_input(tmp_path,monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session,"request",lambda *a,**k:pytest.fail("offline replay must not request a source"))
    from test_pipeline_build_adapter import inputs
    from trading_desk.pipeline_build_adapter import CoreReadModelBuildAdapter
    from trading_desk.pipeline_verify_adapter import CoreReadModelVerifyAdapter
    adapter,args,_=inputs(tmp_path)
    # Use fixture's already bound date/identity, not a second custom runner.
    value=source();data=canonical_json_bytes(value);adapter.snapshot_path.write_bytes(data)
    args["config"]={"snapshotSha256":hashlib.sha256(data).hexdigest()};args["input_sha256"]=canonical_sha256(args["config"])
    result=adapter.run(**args)
    adapter.snapshot_path.unlink()
    assert adapter.run(**args)==result
    config={"buildInputSha256":args["input_sha256"],"buildOutputSha256":result["outputSha256"]}
    verify=CoreReadModelVerifyAdapter(args["workspace"])
    options=dict(context=args["context"],config=config,input_sha256=canonical_sha256(config),workspace=tmp_path/"verify")
    checked=verify.run(**options)
    assert verify.run(**options)==checked
    assert checked["evidence"]["publicationPerformed"] is False
    assert checked["evidence"]["sourceRequestCount"]==0


def test_generated_candidate_passes_actual_typescript_manifest_contract(tmp_path):
    _,manifest=build_market_read_models(source(),require_materialized=True,
        capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    path=tmp_path/"manifest.json";path.write_bytes(canonical_json_bytes(manifest))
    module=(Path(__file__).resolve().parents[2]/"web/app/lib/read-model-contract.ts").as_uri()
    code=f'import {{isReadModelManifest,isCandidateOnlyReadModelManifest}} from "{module}"; import fs from "node:fs"; const m=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); if (!isReadModelManifest(m)||!isCandidateOnlyReadModelManifest(m)) process.exit(1);'
    subprocess.run(["node","--experimental-strip-types","--input-type=module","-e",code,str(path)],check=True,capture_output=True,timeout=20)


def test_snapshot_command_builds_same_replay_candidate(tmp_path):
    source_path=tmp_path/"source.json";source_path.write_bytes(canonical_json_bytes(source()))
    root=Path(__file__).resolve().parents[2]
    import os,sys
    env={**os.environ,"PYTHONPATH":"src:."}
    result=subprocess.run([sys.executable,"scripts/build_read_models.py","--snapshot",str(source_path),"--output-dir",str(tmp_path/"models"),"--slot","dashboard","--require-materialized","--capability-contract-v07"],cwd=root,env=env,check=True,capture_output=True,timeout=30)
    manifest=json.loads(result.stdout)
    assert manifest["capabilityContract"]["capabilities"]["etf_daily_core"]["status"]=="READY"
    rotation=json.loads((tmp_path/"models/rotation.json").read_text())
    assert rotation["data"]["windowEvidence"]["periods"]["3d"]["expectedChanges"]==3


def test_existing_state_command_accepts_calendar_without_promoting_pending(tmp_path):
    from test_read_model_capabilities import _state
    import os,sys
    state=_state();value=_snapshot();value["publication"]["status"]="pending"
    dates=state["dates"]
    calendar={"sourceId":"sse_official_trading_calendar","sha256":"a"*64,"dates":dates,"datesSha256":canonical_sha256(dates)}
    for name,payload in (("state",state),("source",value),("calendar",calendar)):
        (tmp_path/(name+".json")).write_bytes(canonical_json_bytes(payload))
    root=Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable,"scripts/rebuild_dashboard_snapshot_from_state.py",
        "--snapshot",str(tmp_path/"source.json"),"--state",str(tmp_path/"state.json"),
        "--official-calendar",str(tmp_path/"calendar.json"),"--as-of",dates[-1],"--output",str(tmp_path/"out.json")],
        cwd=root,env={**os.environ,"PYTHONPATH":"src:."},check=True,capture_output=True,timeout=30)
    output=json.loads((tmp_path/"out.json").read_text())
    assert output["publication"]["status"]=="pending"
    with pytest.raises(ReadModelError,match="candidate_contract_required"):
        build_market_read_models(output)
