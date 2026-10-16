import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import zipfile

import pytest

from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_entry import (ARCHIVE_REPLAY_SOURCE_CONTRACT, archive_replay_source_binding,
    hydrate_archive_replay_source, prepare_workflow_inputs, prepare_core_candidate,
    candidate_preparation_cost_upper, load_candidate_cost_policy)
from trading_desk.pipeline_runner import PipelineRunnerError
from trading_desk.read_models import canonical_json_bytes
from trading_desk.bounded_http import BoundedHTTPExchange
from test_etf_replay import source
from test_pipeline_preparation import inputs, ENV, NOW
from test_pipeline_entry import ROOT, LOCK, RUNTIME_LOCK, sha


def replay_inputs(tmp_path):
    package, request, path, plan = inputs(tmp_path)
    value = source()
    profile = value.pop("etfReplayInput")
    base = canonical_json_bytes(value)
    calendar = canonical_json_bytes(profile["calendar"])
    rows = profile["partitions"]
    recipe = {"contract":ARCHIVE_REPLAY_SOURCE_CONTRACT,
        "baseSnapshotSha256":hashlib.sha256(base).hexdigest(),
        "calendarSha256":hashlib.sha256(calendar).hexdigest(),
        "calendarGzipBase64":base64.b64encode(gzip.compress(calendar,mtime=0)).decode(),
        "partitions":[{k:r[k] for k in ("sourceId","tradeDate","objectKey","sha256")} for r in rows]}
    final = canonical_json_bytes({**value,"etfReplayInput":profile})
    request["archiveReplaySource"] = recipe
    request["stageConfigs"] = {"build":{"snapshotSha256":hashlib.sha256(final).hexdigest()}}
    path.write_bytes(final)
    plan["budgetManifest"]["archiveReplaySource"] = archive_replay_source_binding(recipe)
    from trading_desk.pipeline_checkpoint_preflight import FreshRecoveryCandidateRequestProfile
    policy = load_candidate_cost_policy(ROOT,profile=FreshRecoveryCandidateRequestProfile("a"*64,7,
        recovery_id="preparation-fixture",plan_sha256="b"*64))
    extra = candidate_preparation_cost_upper(policy,archive_requests=len(rows))
    plan["budgetManifest"]["candidatePreparationUpperBound"] = extra.as_dict()
    plan["budgetManifest"]["taskCostUpperBound"] = policy["task"].add(extra).as_dict()
    plan["deadline"] = (datetime.now(timezone.utc)+timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rebind(package,request,plan)
    return package,request,path,plan,base,rows


def rebind(package,request,plan):
    plan["planHash"] = canonical_sha256({k:v for k,v in plan.items() if k!="planHash"})
    request["recovery"]["planSha256"] = plan["planHash"]
    request["authorization"]["planSha256"] = plan["planHash"]
    from trading_desk.recovery_execution import plan_bytes
    package.update(requestSha256=canonical_sha256(request),planSha256=plan["planHash"],
        scopeSha256=plan_bytes(plan)[1],snapshotSha256=request["stageConfigs"]["build"]["snapshotSha256"])


class Reply:
    def __init__(self,status,data,headers=None):
        self.status_code,self.data,self.headers=status,data,headers or {}
    def iter_content(self,chunk_size): yield self.data
    def close(self): pass


class ArchiveSession:
    def __init__(self,base,rows,fault=None):
        self.rows,self.calls,self.fault=rows,[],fault
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w") as z: z.writestr("dashboard-snapshot.json",base)
        self.archive=out.getvalue()
    def request(self,method,url,**kw):
        self.calls.append((method,url,kw))
        assert method=="GET" and kw["allow_redirects"] is False
        if url.endswith("/artifacts/17"):
            assert kw["headers"]["Authorization"]=="Bearer fixture-github"
            assert "CF-Access-Client-Secret" not in kw["headers"]
            return Reply(200,json.dumps({"id":17,"expired":False,"name":"market-data-collection-source-9",
                "workflow_run":{"id":9},"size_in_bytes":len(self.archive)}).encode())
        if url.endswith("/17/zip"):
            return Reply(302,b"",{"Location":"https://fixture.blob.core.windows.net/source"})
        if url=="https://fixture.blob.core.windows.net/source":
            assert not kw["headers"]
            return Reply(200,self.archive)
        assert url=="https://flow.xiaoker.com/api/v1/admin/history-artifact"
        assert kw["headers"]["Authorization"]=="Bearer fixture-only"
        p=kw["params"]
        assert p["indexed"]=="1" and p["file"]=="data"
        row=next(r for r in self.rows if r["sourceId"]==p["source"] and r["tradeDate"]==p["date"])
        assert p["revision"] == row["objectKey"].split("/")[-2]
        data=base64.b64decode(row["contentBase64"])
        status=404 if self.fault=="missing" else 403 if self.fault=="forbidden" else 200
        if self.fault=="lost": raise OSError("fixture-secret-must-not-leak")
        return Reply(status,b"wrong" if self.fault=="hash" else data,
            {"x-flow-normalized-key":"wrong" if self.fault=="key" else row["objectKey"],
             "x-flow-normalized-sha256":row["sha256"]})
    def close(self): pass


def source_env(package,request,plan):
    return {**ENV,"GITHUB_TOKEN":"fixture-github","FLOW_ENTRY_REQUEST_JSON":json.dumps(request),
        "FLOW_ENTRY_RECOVERY_PLAN_JSON":json.dumps(plan),"FLOW_ENTRY_PREPARATION_JSON":json.dumps(package),
        "FLOW_ENTRY_PREPARATION_SHA256":canonical_sha256(package)}


def test_formal_source_entry_hydrates_and_actual_candidate_graph_is_bound(tmp_path,monkeypatch):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    package["expiresAt"]=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    transport=ArchiveSession(base,rows)
    monkeypatch.setattr("trading_desk.http.build_session",lambda settings:transport)
    destination=tmp_path/"prepared"
    report=prepare_workflow_inputs(destination,artifact_id="17",source_run_id="9",
        environment=source_env(package,request,plan))
    assert report["serverAuthorityVerified"] is False
    assert report["sourceReadRequests"] == {"github":3,"archive":len(rows)}
    assert report["snapshotSha256"]==hashlib.sha256(path.read_bytes()).hexdigest()
    assert (destination/"dashboard-snapshot.json").read_bytes()==path.read_bytes()
    assert len(transport.calls)==3+len(rows)
    graph,context,manifest=prepare_core_candidate(request,snapshot_path=path,producer_build_sha=sha(),
        dependency_lock_paths=[LOCK,RUNTIME_LOCK],project_root=ROOT,now=NOW)
    assert manifest["capabilityContract"]["capabilities"]["etf_daily_core"]["status"]=="READY"
    assert graph["stageConfigs"]["build"]==request["stageConfigs"]["build"]
    assert context.identity.target_date=="2026-08-06"


@pytest.mark.parametrize("fault",["missing","forbidden","hash","key","lost"])
def test_archive_fault_is_bounded_and_does_not_save_partial_source(tmp_path,monkeypatch,fault):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    package["expiresAt"]=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    transport=ArchiveSession(base,rows,fault)
    monkeypatch.setattr("trading_desk.http.build_session",lambda settings:transport)
    with pytest.raises(PipelineRunnerError) as rejected:
        prepare_workflow_inputs(tmp_path/"prepared",artifact_id="17",source_run_id="9",environment=source_env(package,request,plan))
    assert "fixture-secret" not in str(rejected.value)
    assert len(transport.calls)==4
    assert not (tmp_path/"prepared").exists()


@pytest.mark.parametrize("fault",["approval","confirmation","expired","too_many","deadline","request_plan","unknown_budget","low_budget"])
def test_recipe_authority_is_checked_before_any_network(tmp_path,monkeypatch,fault):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    package["expiresAt"]=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    if fault=="approval": plan["budgetManifest"].pop("archiveReplaySource")
    if fault=="expired": package["expiresAt"]="2026-01-01T00:00:00Z"
    if fault=="too_many": request["archiveReplaySource"]["partitions"]*=3
    if fault=="deadline":
        plan["deadline"]="2026-01-01T00:00:00Z"
        rebind(package,request,plan)
    if fault=="request_plan":
        request["recovery"]["planSha256"]="0"*64
        package["requestSha256"]=canonical_sha256(request)
    if fault=="unknown_budget":package["accountReconciliation"]["evidence"]["r2ClassB"]["value"]=None
    if fault=="low_budget":
        plan["budgetManifest"]["taskCostUpperBound"]["workerRequests"]["value"]=1
        rebind(package,request,plan)
    env=source_env(package,request,plan)
    if fault=="confirmation":env["FLOW_ENTRY_PREPARATION_SHA256"]="0"*64
    monkeypatch.setattr("trading_desk.http.build_session",lambda settings:pytest.fail("invalid scope must not request"))
    with pytest.raises(PipelineRunnerError):
        prepare_workflow_inputs(tmp_path/"prepared",artifact_id="17",source_run_id="9",environment=env)
    assert not (tmp_path/"prepared").exists()


@pytest.mark.parametrize("fault",["calendar_hash","compressed","duplicate","date","final_hash"])
def test_exact_recipe_and_final_snapshot_cannot_drift(tmp_path,fault):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    recipe=request["archiveReplaySource"]
    if fault=="calendar_hash":recipe["calendarSha256"]="0"*64
    if fault=="compressed":recipe["calendarGzipBase64"]="not-gzip"
    if fault=="duplicate":recipe["partitions"][1]=deepcopy(recipe["partitions"][0])
    if fault=="date":recipe["partitions"][0]["tradeDate"]="2026-08-02"
    if fault=="final_hash":request["stageConfigs"]["build"]["snapshotSha256"]="0"*64
    transport=ArchiveSession(base,rows)
    with pytest.raises(PipelineRunnerError):
        hydrate_archive_replay_source(json.loads(base),request,exchange=BoundedHTTPExchange(transport),environment=ENV)
    assert len(transport.calls)==(len(rows) if fault=="final_hash" else 0)


def test_archive_preparation_cost_is_reserved_once_for_both_attempts(tmp_path):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    from trading_desk.pipeline_checkpoint_preflight import FreshRecoveryCandidateRequestProfile,ResumeRecoveryCandidateRequestProfile
    args={"recovery_id":plan["recoveryId"],"plan_sha256":plan["planHash"]}
    first=load_candidate_cost_policy(ROOT,profile=FreshRecoveryCandidateRequestProfile("a"*64,7,**args),approved_recovery_plan=plan)
    second=load_candidate_cost_policy(ROOT,profile=ResumeRecoveryCandidateRequestProfile("a"*64,7,**args),approved_recovery_plan=plan)
    assert first["task"].numeric()==second["task"].numeric()
    assert first["task"].metrics["workerRequests"].value==740+8+2*len(rows)
    bad=deepcopy(plan);bad["budgetManifest"]["archiveReplaySource"]["requestCount"]-=1
    with pytest.raises(PipelineRunnerError):
        load_candidate_cost_policy(ROOT,profile=FreshRecoveryCandidateRequestProfile("a"*64,7,**args),approved_recovery_plan=bad)


def test_replay_scope_uses_existing_preparation_and_shared_budget(tmp_path):
    from test_pipeline_preparation import Sequence
    from trading_desk.pipeline_preparation import prepare_candidate_authority
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    transport=Sequence(package,request)
    report=prepare_candidate_authority(package,canonical_sha256(package),request,
        snapshot_path=path,approved_plan=plan,producer_build_sha=sha(),project_root=ROOT,
        output_path=tmp_path/"authority.json",environment=ENV,now=NOW,
        session=transport,allow_loopback_fixture=True)
    assert report["status"] == "CANDIDATE_AUTHORITY_PREPARED"
    assert report["combinedUpperBound"]["workerRequests"]["value"] == 740+8+2*len(rows)
    assert report["candidateExecuted"] is report["activeReleaseChanged"] is False


def test_interrupted_before_build_new_process_reads_fit_initial_hold(tmp_path,monkeypatch):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    package["expiresAt"]=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    calls=[]
    def session(settings):
        transport=ArchiveSession(base,rows)
        calls.append(transport)
        return transport
    monkeypatch.setattr("trading_desk.http.build_session",session)
    # No build/checkpoint is saved between independently created transports.
    for attempt in (1,2):
        prepare_workflow_inputs(tmp_path/f"attempt-{attempt}",artifact_id="17",source_run_id="9",
            environment=source_env(package,request,plan))
    archive_reads=sum(len(t.calls)-3 for t in calls)
    assert archive_reads == 2*len(rows)
    assert plan["budgetManifest"]["candidatePreparationUpperBound"]["workerRequests"]["value"] == 8+archive_reads


@pytest.mark.parametrize("fault", ["expanded_calendar", "csv_schema", "csv_date"])
def test_untrusted_archive_content_is_bounded_and_validated(tmp_path, fault):
    package,request,path,plan,base,rows=replay_inputs(tmp_path)
    if fault == "expanded_calendar":
        data = b" " * 2_800_001
        request["archiveReplaySource"]["calendarGzipBase64"] = base64.b64encode(gzip.compress(data,mtime=0)).decode()
        request["archiveReplaySource"]["calendarSha256"] = hashlib.sha256(data).hexdigest()
    else:
        data = base64.b64decode(rows[0]["contentBase64"])
        data = data.replace(b"shares", b"unknown") if fault == "csv_schema" else data.replace(b"2026-08-03", b"2026-08-02")
        rows[0]["contentBase64"] = base64.b64encode(data).decode()
        rows[0]["sha256"] = hashlib.sha256(data).hexdigest()
        request["archiveReplaySource"]["partitions"][0]["sha256"] = rows[0]["sha256"]
    transport=ArchiveSession(base,rows)
    with pytest.raises(PipelineRunnerError):
        hydrate_archive_replay_source(json.loads(base),request,exchange=BoundedHTTPExchange(transport),environment=ENV)
    assert len(transport.calls) <= len(rows)
