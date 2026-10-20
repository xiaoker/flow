"""Synthetic official-shaped bytes exercise the real archive validators.

These fixtures are not evidence of production dates, authorization or usage.
"""
import base64
from copy import deepcopy
from dataclasses import asdict
import gzip
import hashlib
import io
import json

import pandas as pd
import pytest

from trading_desk.adapters.official import SSE_ETF, SZSE_ETF, SSE_MARGIN, SSE_MARGIN_SECURITY, SZSE_MARGIN, BSE_MARGIN, HKEX_CONNECT
from trading_desk.models import FetchArtifact
from trading_desk.phase0_archive import ArchivedOfficialMarketAdapters, validate_recipe, CONTRACT
from trading_desk.phase0 import collect_phase0
from trading_desk.pipeline_application import canonical_sha256
from trading_desk import release_qa as qa

DATES = ["2026-07-14", "2026-07-15", "2026-07-16"]


def fixture_recipe(*, closed_days=()):
    bodies, refs, artifacts = {}, [], []

    def add(source, day, content, frames, extension="json"):
        sha = hashlib.sha256(content).hexdigest()
        prefix = f"{source.source_id}-{source.dataset}-{day}"
        raw_path = f"data/raw/{prefix}.{extension}"
        metadata_path = f"data/raw/{prefix}.metadata.json"
        paths = [f"data/normalized/{prefix}-{i}.csv" for i in range(len(frames))]
        artifact = FetchArtifact(source, day, "2026-07-17T01:00:00Z", 200, "application/json",
            raw_path, metadata_path, sha, sum(len(f) for f in frames), ";".join(paths))
        item = artifact.to_dict()
        artifacts.append(item)
        metadata = json.dumps({k:v for k,v in item.items() if k not in {"raw_path", "metadata_path", "normalized_path"}}).encode()
        names = ["summary", "top"] if len(frames) == 2 else ["data"]
        entries = [(raw_path, f"market-data/raw/{source.source_id}/{source.dataset}/{day}/{sha}.{extension}", content),
            (metadata_path, f"market-data/raw/{source.source_id}/{source.dataset}/{day}/{sha}.metadata.json", metadata)]
        for path, frame, name in zip(paths, frames, names, strict=True):
            frame = frame.copy()
            if source.dataset == "etf_scale":
                frame["date_basis"] = "source_published" if source.source_id == "sse" else "source_page_previous_trading_day"
            if source.source_id == "bse":
                frame["source_trade_date"] = day
            frame["source_id"], frame["source_grade"], frame["observed_at"] = source.source_id, "A", artifact.observed_at
            data = frame.to_csv(index=False).encode()
            entries.append((path, f"market-data/normalized/{source.source_id}/{source.dataset}/{day}/{sha}/{name}.csv", data))
        for path, key, data in entries:
            bodies[key] = data
            refs.append({"path":path,"objectKey":key,"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)})

    for i, day in enumerate(DATES):
        raw = json.dumps({"result":[{"STAT_DATE":day,"SEC_CODE":"510300","SEC_NAME":"沪深300ETF","TOT_VOL":10000+i*10}]}).encode()
        add(SSE_ETF,day,raw,[qa._expected_sse_etf(raw,day)])
        raw = json.dumps({"result":[{"opDate":day,"stockCode":"600000","securityAbbr":"浦发银行",
            "rzye":100,"rzmre":10,"rzche":5,"rqyl":2,"rqmcl":1,"rqchl":1}]}).encode()
        add(SSE_MARGIN_SECURITY,day,raw,[qa._expected_sse_margin_security(raw,day)])
        raw = json.dumps([{"metadata":{"subname":day},"data":[{"jrrzye":"10","jrrzmr":"1","jrrjye":"1","jrrzrjye":"11"}]}]).encode()
        add(SZSE_MARGIN,day,raw,[qa._expected_szse_margin(raw,day)])
        raw = ("null([[{\"zqdm\":\"999999\",\"rzye\":100,\"rzmre\":10,\"rqye\":5,\"rqyl\":2,\"rqmcl\":1,\"rzrqye\":105}]], '"+day+"')").encode()
        add(BSE_MARGIN,day,raw,[qa._expected_bse_margin(raw,day)],"jsonp")
        summary_schema = ["Total Turnover","Total Trade Count","ETF Turnover"]
        payload = [{"date":day,"market":"SSE Northbound","tradingDay":1,"content":[
            {"style":1,"table":{"schema":[summary_schema],"tr":[{"td":[[v]]} for v in ["100","10","5"]]}},
            {"style":2,"table":{"schema":[["Rank","Stock Code","Stock Name","Buy Turnover","Sell Turnover","Total Turnover"]],
                "tr":[{"td":[[str(rank),str(600000+rank),"测试证券","10","5","15"]]} for rank in range(1,11)]}}]}]
        north = payload[0]
        payload = []
        for market in ("SSE Northbound", "SZSE Northbound", "SSE Southbound", "SZSE Southbound"):
            block = deepcopy(north)
            block["market"] = market
            if "Southbound" in market:
                block["content"][0]["table"] = {"schema":[["Total Turnover","Buy Turnover","Sell Turnover","Total Trade Count","ETF Turnover"]],
                    "tr":[{"td":[[v]]} for v in ["100","60","40","10","5"]]}
            payload.append(block)
        raw = ("tabData = "+json.dumps(payload)+";").encode()
        normalized = list(qa._expected_hkex(raw,day))
        if day in closed_days:
            for block in payload:
                block["tradingDay"] = 0
                block["content"] = []
            raw = ("tabData = "+json.dumps(payload)+";").encode()
            # Match the live adapter's legacy empty, headerless partitions.
            normalized = [pd.DataFrame(), pd.DataFrame()]
        add(HKEX_CONNECT,day,raw,normalized,"js")
    raw = json.dumps({"result":[{"opDate":d.replace("-",""),"rzye":100,"rzmre":10,"rqylje":5,"rzrqjyzl":105} for d in DATES]}).encode()
    add(SSE_MARGIN,f"{DATES[0]}_{DATES[-1]}",raw,[qa._expected_sse_margin(raw)])
    stream = io.BytesIO()
    pd.DataFrame([{"基金代码":"159915","基金简称":"创业板ETF","基金类别":"ETF","当前规模(份)":100000000,"净值":1.0}]).to_excel(stream,index=False)
    raw = stream.getvalue()
    add(SZSE_ETF,DATES[-1],raw,[qa._expected_szse_etf(raw,DATES[-1])],"xlsx")
    manifest = {"run_id":"fixture","requested_dates":DATES,"artifacts":artifacts}
    encoded = json.dumps(manifest,ensure_ascii=False).encode()
    recipe = {"contract":CONTRACT,"manifestGzipBase64":base64.b64encode(gzip.compress(encoded,mtime=0)).decode(),
        "manifestSha256":hashlib.sha256(encoded).hexdigest(),"files":refs,
        "calendar":{"sourceId":"sse_official_trading_calendar","sha256":"a"*64,"dates":DATES,"datesSha256":canonical_sha256(DATES)}}
    return recipe, bodies


def test_real_lineage_and_mapping_checks_and_resume_without_refetch(tmp_path):
    recipe, bodies = fixture_recipe()
    calls = []
    def fetch(ref):
        calls.append(ref["objectKey"])
        return bodies[ref["objectKey"]]
    adapter = ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",fetch=fetch)
    assert len(calls) == len({ref["sha256"] for ref in recipe["files"]})
    assert adapter.fetch_sse_etf(DATES[-1])[0].iloc[0].shares == 100200000
    resumed = ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",
        fetch=lambda ref:pytest.fail("validated cache must survive a new adapter instance"))
    assert resumed.fetch_hkex_connect(DATES[-1])[0].iloc[0].total_turnover == 100_000_000
    assert resumed.fetch_sse_etf_pcf([],DATES[-1]) is None
    with pytest.raises(ValueError,match="archive_phase0_input_missing"):
        resumed.fetch_sse_etf("2026-07-13")
    class NoNetwork:
        def request(self,*a,**k): pytest.fail("archive mode cannot call official sources")
    result = collect_phase0(dates=DATES,data_root=tmp_path/"data",report_root=tmp_path/"reports",
        workspace_root=tmp_path,collection_window="reconcile",archived_adapters=resumed,session=NoNetwork())
    assert result["quality"]["counts"]["fail"] == 0
    assert result["quality"]["errors"] == []
    assert result["manifest"]["requested_dates"] == DATES


@pytest.mark.parametrize("fault", ["hash","missing","extra","path","date","key","size","duplicate","calendar"])
def test_recipe_rejects_static_faults_before_any_fetch(tmp_path,fault):
    recipe,_ = fixture_recipe()
    if fault == "hash": recipe["manifestSha256"] = "0"*64
    if fault == "missing": recipe["files"].pop()
    if fault == "extra": recipe["files"].append(deepcopy(recipe["files"][0]))
    if fault == "path": recipe["files"][0]["path"] = "../protected"
    if fault == "date": recipe["files"][0]["objectKey"] = recipe["files"][0]["objectKey"].replace(DATES[0],"2026-07-13")
    if fault == "key": recipe["files"][0]["objectKey"] = "market-data/private/key"
    if fault == "size": recipe["files"][0]["bytes"] = 2_000_001
    if fault == "duplicate": recipe["files"][1] = deepcopy(recipe["files"][0])
    if fault == "calendar": recipe["calendar"]["datesSha256"] = "0"*64
    with pytest.raises(ValueError):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",fetch=lambda ref:pytest.fail("must reject before IO"))
    assert not (tmp_path/"archive").exists()


def test_response_loss_keeps_verified_files_and_wrong_bytes_never_reach_adapter(tmp_path):
    recipe,bodies = fixture_recipe()
    calls = []
    def broken(ref):
        calls.append(ref["objectKey"])
        if len(calls) == 3: raise TimeoutError("lost read")
        return bodies[ref["objectKey"]]
    with pytest.raises(TimeoutError):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",fetch=broken)
    restored = []
    adapter = ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",
        fetch=lambda ref:(restored.append(ref["objectKey"]),bodies[ref["objectKey"]])[1])
    assert calls[0] not in restored and calls[1] not in restored
    assert adapter.fetch_szse_etf_current(DATES)[0].iloc[0].instrument_code == "159915"
    with pytest.raises(ValueError,match="archive_phase0_object_mismatch"):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"bad",fetch=lambda ref:b"corrupt")


def test_valid_hashes_do_not_replace_raw_to_normalized_business_qa(tmp_path):
    recipe,bodies = fixture_recipe()
    ref = next(ref for ref in recipe["files"] if ref["path"].startswith("data/normalized/sse-etf_scale"))
    changed = bodies[ref["objectKey"]].replace(b"100000000",b"990000000")
    assert changed != bodies[ref["objectKey"]]
    bodies[ref["objectKey"]] = changed
    ref["sha256"],ref["bytes"] = hashlib.sha256(changed).hexdigest(),len(changed)
    with pytest.raises(ValueError,match="archive_phase0_lineage_invalid"):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"bad-business",fetch=lambda ref:bodies[ref["objectKey"]])


def test_tampered_cache_and_symlink_destinations_fail_closed(tmp_path):
    recipe, bodies = fixture_recipe()
    root = tmp_path / "archive"
    ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=root,fetch=lambda ref:bodies[ref["objectKey"]])
    (root / ".archive-wire" / recipe["files"][0]["sha256"]).write_bytes(b"tampered")
    with pytest.raises(ValueError,match="archive_phase0_object_mismatch"):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=root,fetch=lambda ref:pytest.fail("do not hide cache tampering by fetching again"))
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match="archive_phase0_destination_invalid"):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=linked,fetch=lambda ref:pytest.fail("reject before fetch"))
    assert list(outside.iterdir()) == []


def test_manifest_expansion_limit_precedes_hash_or_remote_reads(tmp_path):
    recipe, _ = fixture_recipe()
    oversized = b" " * 256_001
    recipe["manifestGzipBase64"] = base64.b64encode(gzip.compress(oversized,mtime=0)).decode()
    recipe["manifestSha256"] = hashlib.sha256(oversized).hexdigest()
    with pytest.raises(ValueError,match="archive_phase0_manifest_invalid"):
        ArchivedOfficialMarketAdapters(recipe,target=DATES[-1],root=tmp_path/"archive",fetch=lambda ref:pytest.fail("oversized manifest must fail before fetch"))
    assert not (tmp_path/"archive").exists()
