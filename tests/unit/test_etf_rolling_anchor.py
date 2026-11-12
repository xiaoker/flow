from datetime import date, timedelta
from hashlib import sha256
import json
import pytest

from trading_desk.etf_flow_state import materialize_etf_terminal
from trading_desk.etf_rolling_refs import execute_daily_reuse, plan_daily_reuse, resolve_rolling_refs, select_rolling_refs
from trading_desk.pipeline_application import canonical_sha256
from trading_desk.etf_rolling_refs import decode_daily_cache, encode_daily_cache, materialize_rolling_daily
from trading_desk.etf_rolling_refs import build_rolling_candidate
from trading_desk.etf_rolling_refs import pack_rolling_checkpoint, unpack_rolling_checkpoint
from trading_desk.read_models import canonical_json_bytes
from trading_desk.etf_rolling_refs import validate_published_checkpoint_ref
from trading_desk.etf_rolling_refs import resolve_rolling_binding


def rolling_production_fixture():
    from test_read_models import _snapshot, _state
    from trading_desk.etf_flow_state import materialize_dashboard_snapshot

    end = date(2026, 8, 5)
    dates = [(end - timedelta(days=offset)).isoformat() for offset in range(40, -1, -1)
             if (end - timedelta(days=offset)).weekday() < 5][-22:]
    target = dates[-1]
    calendar = {"sourceId": "sse_official_trading_calendar", "sha256": "a" * 64,
        "dates": dates, "datesSha256": canonical_sha256(dates)}
    state = _state()
    state["dates"] = dates[-2:]
    for row in state["instruments"]:
        row["values"], row["cashValues"] = [1, 1], [None, None]
    base = materialize_dashboard_snapshot(_snapshot(), state, as_of=target)
    base["publication"] = {"status": "confirmed"}
    base["observedAt"] = target + "T09:00:00+08:00"
    refs = [{"sourceId": source, "tradeDate": day, "sha256": "b" * 64,
        "objectKey": f"market-data/normalized/{source}/etf_scale/{day}/{'c' * 64}/data.csv"}
        for day in dates for source in ("sse", "szse")]
    by_key = {(r["sourceId"], r["tradeDate"]): r for r in refs}
    nodes = {}
    for index, day in enumerate(dates[1:], 1):
        for source, code in (("sse", "510300"), ("szse", "159915")):
            value = {"instruments": [{"market": source.upper(), "code": code, "periods": {"1d": 1}}]}
            nodes[source, day] = {"inputSha256": canonical_sha256([
                "fixture-v1", by_key[source, dates[index - 1]], by_key[source, day]]),
                "valueSha256": canonical_sha256(value), "value": value}
    ref_bytes, cache = canonical_json_bytes(refs), encode_daily_cache(nodes)
    wire = pack_rolling_checkpoint({"version": 1, "asOf": target,
        "calculationVersion": "fixture-v1", "refsSha256": sha256(ref_bytes).hexdigest(),
        "cacheSha256": sha256(cache).hexdigest()}, ref_bytes, cache)
    inputs = dict(calendar=calendar, checkpoint=wire,
        checkpoint_sha256=sha256(wire).hexdigest(), calculation_version="fixture-v1")
    return base, inputs


@pytest.mark.parametrize("producer", ["d" * 40, sha256(b"fixture-github-commit").hexdigest()])
def test_native_production_rebuild_is_not_candidate_relabeling(producer):
    from copy import deepcopy
    from trading_desk.read_models import build_market_read_models, ReadModelError
    from trading_desk.read_model_capabilities import CAPABILITY_CONTRACT_VERSION
    base, inputs = rolling_production_fixture()
    wire = inputs["checkpoint"]
    original = deepcopy(base)
    payloads, manifest = build_market_read_models(base, require_materialized=True,
        producer_build_sha=producer, rolling_publication_input=inputs)
    assert base == original
    assert manifest["schemaVersion"] == 3 and "capabilityContract" not in manifest
    assert manifest["rollingPublication"]["status"] == "READY"
    assert manifest["producerBuildSha"] == producer
    assert manifest["rollingPublication"]["checkpointSha256"] == sha256(wire).hexdigest()
    terminal = payloads["radar"]["data"]["etfTerminal"]
    for row in terminal["instruments"]:
        assert row["periods"]["2w"] == 10 and row["periods"]["1m"] == 21
        assert row["estimatedCashPeriods"].get("1m") is None
    with pytest.raises(ValueError, match="requires_source_snapshot"):
        build_market_read_models({**base, "etfTerminal": terminal}, producer_build_sha="d" * 40,
            rolling_publication_input=inputs)
    with pytest.raises(ReadModelError, match="candidate_conflict"):
        build_market_read_models(base, producer_build_sha="d" * 40,
            rolling_publication_input=inputs, capability_contract_version=CAPABILITY_CONTRACT_VERSION)
    with pytest.raises(ValueError, match="object_identity_invalid"):
        build_market_read_models(base, producer_build_sha="d" * 40,
            rolling_publication_input={**inputs, "checkpoint": wire + b"x"})


@pytest.mark.parametrize("corrupt", [None, "snapshot", "calendar", "checkpoint"])
def test_publisher_cli_verifies_immutable_rolling_bundle(tmp_path, monkeypatch, capsys, corrupt):
    import sys
    from scripts.build_read_models import main
    base, inputs = rolling_production_fixture()
    source = canonical_json_bytes(base)
    calendar = canonical_json_bytes(inputs["calendar"])
    checkpoint = inputs["checkpoint"]
    (tmp_path / "dashboard-snapshot.json").write_bytes(source)
    (tmp_path / "rolling-calendar.json").write_bytes(calendar)
    (tmp_path / "rolling-checkpoint.gz").write_bytes(checkpoint)
    descriptor = {"version": 1, "asOf": base["asOf"], "calculationVersion": "fixture-v1",
        "snapshotSha256": sha256(source).hexdigest(), "calendarSha256": sha256(calendar).hexdigest(),
        "checkpointSha256": sha256(checkpoint).hexdigest()}
    (tmp_path / "rolling-publication-input.json").write_bytes(canonical_json_bytes(descriptor))
    if corrupt:
        path = {"snapshot": "dashboard-snapshot.json", "calendar": "rolling-calendar.json",
                "checkpoint": "rolling-checkpoint.gz"}[corrupt]
        with (tmp_path / path).open("ab") as stream:
            stream.write(b" ")
    output = tmp_path / "models"
    monkeypatch.setattr(sys, "argv", ["build_read_models", "--slot", "dashboard",
        "--snapshot", str(tmp_path / "dashboard-snapshot.json"), "--output-dir", str(output),
        "--rolling-input", str(tmp_path / "rolling-publication-input.json"),
        "--producer-build-sha", "d" * 40, "--require-materialized"])
    if corrupt:
        with pytest.raises((SystemExit, ValueError)):
            main()
        assert not output.exists()
    else:
        main()
        manifest = json.loads(capsys.readouterr().out)
        assert manifest["schemaVersion"] == 3
        assert manifest["rollingPublication"]["checkpointSha256"] == descriptor["checkpointSha256"]
        assert len(list(output.glob("*.json"))) == 8


def test_unavailable_rolling_proof_keeps_daily_facts_and_never_fills_unproven_cells():
    from copy import deepcopy
    from trading_desk.read_models import build_market_read_models
    base, _ = rolling_production_fixture()
    original = deepcopy(base)
    payloads, manifest = build_market_read_models(base, require_materialized=True,
        producer_build_sha="d" * 64, rolling_unavailable_reason="rolling_proof_unavailable")
    assert base == original
    assert manifest["rollingPublication"]["status"] == "UNAVAILABLE"
    assert "checkpointSha256" not in manifest["rollingPublication"]
    rows = payloads["radar"]["data"]["etfTerminal"]["instruments"]
    old = {(r["market"], r["code"]): r for r in base["etfTerminal"]["instruments"]}
    for row in rows:
        assert row["periods"]["1d"] == old[row["market"], row["code"]]["periods"]["1d"]
        assert row["periods"]["2w"] is None and row["periods"]["1m"] is None
    with pytest.raises(ValueError, match="unavailable_source_invalid"):
        build_market_read_models(base, producer_build_sha="d" * 64, rolling_unavailable_reason="ignore_qa")


@pytest.mark.parametrize("reason,fatal", [("request_unavailable", False),
    ("rolling_collected_target_not_observed", False),
    ("rolling_collected_source_identity_missing", True),
    ("rolling_collected_source_identity_invalid", True), ("archive_recipe_calendar_source_hash_mismatch", True)])
def test_preparation_report_separates_optional_readiness_from_current_source_failure(tmp_path, monkeypatch, capsys, reason, fatal):
    import sys
    import scripts.prepare_rolling_etf_publication as entry
    base, _ = rolling_production_fixture()
    source = canonical_json_bytes(base)
    (tmp_path / "source.json").write_bytes(source)
    for name in ("manifest", "history"):
        (tmp_path / f"{name}.json").write_text("{}")
    report = tmp_path / "status.json"
    monkeypatch.setattr(sys, "argv", ["prepare", "--snapshot", str(tmp_path / "source.json"),
        "--manifest", str(tmp_path / "manifest.json"), "--history-bundle", str(tmp_path / "history.json"),
        "--output", str(tmp_path / "ready"), "--report", str(report)])
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture")
    def fail(*args, **kwargs):
        raise ValueError(reason)
    monkeypatch.setattr(entry, "prepare", fail)
    if fatal:
        with pytest.raises(SystemExit):
            entry.main()
    else:
        entry.main()
    value = json.loads(report.read_bytes())
    assert value["status"] == ("BLOCKED_SOURCE" if fatal else "UNAVAILABLE")
    assert value["snapshotSha256"] == sha256(source).hexdigest()
    assert value["workerRequests"] == 0 and value["d1RowsRead"] is None
    assert not (tmp_path / "ready").exists()
    capsys.readouterr()
    from scripts.build_read_models import main as publish_build
    models = tmp_path / "models"
    monkeypatch.setattr(sys, "argv", ["build", "--slot", "dashboard", "--snapshot", str(tmp_path / "source.json"),
        "--output-dir", str(models), "--rolling-unavailable-status", str(report),
        "--producer-build-sha", "a" * 64, "--require-materialized"])
    if fatal:
        with pytest.raises(SystemExit):
            publish_build()
        assert not models.exists()
    else:
        publish_build()
        result = json.loads(capsys.readouterr().out)
        assert result["rollingPublication"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("fault", ["transport", "status", "body"])
def test_checkpoint_binding_uncertainty_never_retries(fault):
    from types import SimpleNamespace
    from scripts.bind_rolling_checkpoint import bind_checkpoint
    from trading_desk.bounded_http import BoundedHTTPError
    from trading_desk.read_models import build_market_read_models
    base, inputs = rolling_production_fixture()
    _, manifest = build_market_read_models(base, producer_build_sha="d" * 40,
        rolling_publication_input=inputs)
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        if fault == "transport":
            raise BoundedHTTPError("request_unavailable")
        return SimpleNamespace(status=503 if fault == "status" else 200, content=b"{}")
    activation = {"status": "activated", "slot": "dashboard",
        "releaseId": manifest["releaseId"], "asOf": manifest["asOf"]}
    kwargs = dict(manifest=manifest, activation=activation, wire=inputs["checkpoint"],
        execution={}, headers={}, exchange=SimpleNamespace(request=request))
    with pytest.raises(ValueError):
        bind_checkpoint(**kwargs)
    assert len(calls) == 1
    assert bind_checkpoint(**{**kwargs, "activation": {"status": "published_historical"}})["status"] == "SKIPPED"
    assert len(calls) == 1


def test_rolling_official_calendar_cover_stays_bounded_and_rejects_conflicting_proofs():
    import base64
    from scripts.prepare_rolling_etf_publication import extend_calendar
    from trading_desk.etf_period_dependencies import OFFICIAL_MARGIN_DATES_SOURCE
    def proof(days):
        raw = canonical_json_bytes({"beginDate": days[0].replace("-", ""),
            "endDate": days[-1].replace("-", ""),
            "result": [{"opDate": d.replace("-", "")} for d in days],
            "pageHelp": {"pageCount": 1, "total": len(days)}})
        return {"sourceId": OFFICIAL_MARGIN_DATES_SOURCE, "sha256": sha256(raw).hexdigest(),
            "dates": days, "datesSha256": canonical_sha256(days), "evidence": {
                "rangeStart": days[0], "rangeEnd": days[-1],
                "contentBase64": base64.b64encode(raw).decode()}}
    days = [(date(2026, 7, 1) + timedelta(days=i)).isoformat() for i in range(110)
        if (date(2026, 7, 1) + timedelta(days=i)).weekday() < 5]
    previous = proof(days[:22])
    for index in range(22, len(days)):
        previous = extend_calendar(previous, proof(days[index - 2:index + 1]), days[index])
        assert days[index - 21:index + 1] == previous["dates"][-22:]
        assert len(previous.get("evidence", {}).get("calendars", [])) <= 12
    with pytest.raises(ValueError, match="overlap_conflict"):
        extend_calendar(proof(days[:22]), proof([days[19], days[21], days[22]]), days[22])


def test_daily_preparation_cold_start_is_one_read_but_bad_existing_checkpoint_cannot_fallback():
    from types import SimpleNamespace
    from scripts.prepare_rolling_etf_publication import prepare
    base, inputs = rolling_production_fixture()
    calls = []
    def cold(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status=404, content=b"", headers={})
    assert prepare(base, {}, {}, exchange=SimpleNamespace(request=cold), headers={}) == (
        {"status": "BOOTSTRAP_REQUIRED"}, None)
    assert len(calls) == 1
    calls.clear()
    def existing(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status=200, content=inputs["checkpoint"], headers={
            "x-flow-release-id": f"release:{base['asOf']}:" + "a" * 16,
            "x-flow-as-of": base["asOf"], "x-flow-manifest-sha256": "b" * 64,
            "x-flow-checkpoint-sha256": inputs["checkpoint_sha256"]})
    with pytest.raises(ValueError, match="checkpoint_calendar_missing"):
        prepare(base, {}, {}, exchange=SimpleNamespace(request=existing), headers={})
    assert len(calls) == 1


@pytest.mark.parametrize("window_ahead", [False, True])
@pytest.mark.parametrize("asymmetric", [False, True])
def test_daily_collector_preparation_advances_real_builders_without_recollecting(window_ahead, asymmetric):
    import base64
    from types import SimpleNamespace
    from scripts.prepare_rolling_etf_publication import prepare
    from trading_desk.etf_period_dependencies import OFFICIAL_MARGIN_DATES_SOURCE
    from trading_desk.etf_flow_state import materialize_dashboard_snapshot
    from trading_desk.read_models import build_market_read_models
    from test_read_models import _state
    base, prior = rolling_production_fixture()
    old_manifest, refs_bytes, cache_bytes = unpack_rolling_checkpoint(prior["checkpoint"], prior["checkpoint_sha256"])
    old_refs, nodes = json.loads(refs_bytes), decode_daily_cache(cache_bytes)
    dates = prior["calendar"]["dates"] + ["2026-08-06"]
    def calendar(days):
        raw = canonical_json_bytes({"beginDate": days[0].replace("-", ""), "endDate": days[-1].replace("-", ""),
            "result": [{"opDate": day.replace("-", "")} for day in days], "pageHelp": {"pageCount": 1, "total": len(days)}})
        return {"sourceId": OFFICIAL_MARGIN_DATES_SOURCE, "sha256": sha256(raw).hexdigest(),
            "dates": days, "datesSha256": canonical_sha256(days), "evidence": {"rangeStart": days[0],
                "rangeEnd": days[-1], "contentBase64": base64.b64encode(raw).decode()}}
    data, refs = {}, []
    for index, day in enumerate(dates):
        for source, code in (("sse", "510300"), ("szse", "159915")):
            raw = f"trade_date,market,instrument_code,shares\n{day},{source.upper()},{code},{(index + 1) * 100000000}\n".encode()
            digest = sha256(raw).hexdigest()
            raw_sha = sha256(f"raw:{source}:{day}".encode()).hexdigest()
            revision = sha256(f"{raw_sha}:{digest}".encode()).hexdigest()
            data[source, day] = raw
            refs.append({"sourceId": source, "tradeDate": day, "sha256": digest,
                "objectKey": f"market-data/normalized/{source}/etf_scale/{day}/{revision}/data.csv"})
    pinned = {(r["sourceId"], r["tradeDate"]): r for r in refs}
    for (source, day), node in nodes.items():
        node["inputSha256"] = canonical_sha256(["fixture-v1",
            pinned[source, dates[dates.index(day) - 1]], pinned[source, day]])
    previous_refs, previous_cache = canonical_json_bytes(refs[:-2]), encode_daily_cache(nodes)
    old_manifest.update(officialCalendar=calendar(dates[:-1]),
        refsSha256=sha256(previous_refs).hexdigest(), cacheSha256=sha256(previous_cache).hexdigest())
    wire = pack_rolling_checkpoint(old_manifest, previous_refs, previous_cache)
    requested = dates[-3:] if not window_ahead else [dates[-2], dates[-1], "2026-08-07"]
    recent = calendar(dates[-3:] if not window_ahead else dates[-2:])
    if window_ahead:
        raw = json.loads(base64.b64decode(recent["evidence"]["contentBase64"]))
        raw["endDate"] = "20260807"
        content = canonical_json_bytes(raw)
        recent["sha256"] = sha256(content).hexdigest()
        recent["evidence"].update(rangeEnd="2026-08-07", contentBase64=base64.b64encode(content).decode())
    partition = f"{requested[0]}_{requested[-1]}"
    raw_margin = {"sourceId": "sse", "dataset": "margin_market_daily", "requestedDate": partition,
        "sha256": recent["sha256"], "rawContentBase64": recent["evidence"]["contentBase64"]}
    manifest = {"requested_dates": requested, "run_id": "fixture", "artifacts": [{
        "source": {"source_id": "sse", "dataset": "margin_market_daily"},
        "requested_date": partition, "sha256": recent["sha256"]}]}
    bundle = {"version": 1, "history": {"runId": "fixture", "asOf": dates[-1], "artifacts": [raw_margin]}}
    for day in recent["dates"]:
        for source in ("sse", "szse"):
            if asymmetric and source == "szse" and day != dates[-1]:
                continue
            raw = f"raw:{source}:{day}".encode()
            raw_sha = sha256(raw).hexdigest()
            bundle["history"]["artifacts"].append({"sourceId": source, "dataset": "etf_scale",
                "requestedDate": day, "sha256": raw_sha, "rawContentBase64": base64.b64encode(raw).decode(),
                "normalized": [{"name": "data", "tradeDates": [day], "sha256": sha256(data[source, day]).hexdigest(),
                    "contentBase64": base64.b64encode(data[source, day]).decode()}]})
            manifest["artifacts"].append({"source": {"source_id": source, "dataset": "etf_scale"},
                "requested_date": day, "sha256": raw_sha})
    state = _state()
    state["dates"] = dates[-2:]
    for row in state["instruments"]:
        row["values"], row["cashValues"] = [1, 1], [None, None]
    current = materialize_dashboard_snapshot(base, state, as_of=dates[-1])
    current["publication"] = {"status": "confirmed"}
    current["observedAt"] = dates[-1] + "T09:00:00+08:00"
    calls = []
    active = {"wire": wire, "asOf": base["asOf"]}
    def request(method, url, **kwargs):
        assert method == "GET"
        calls.append((url, kwargs["params"]))
        if url.endswith("/etf-state"):
            return SimpleNamespace(status=200, content=active["wire"], headers={
                "x-flow-release-id": f"release:{active['asOf']}:" + "a" * 16,
                "x-flow-as-of": active["asOf"], "x-flow-manifest-sha256": "b" * 64,
                "x-flow-checkpoint-sha256": sha256(active["wire"]).hexdigest()})
        query = kwargs["params"]
        ref = pinned[query["source"], query["date"]]
        if "revision" in query:
            assert asymmetric and query["source"] == "szse" and query["date"] == dates[-2]
            assert query["revision"] == ref["objectKey"].split("/")[-2]
        return SimpleNamespace(status=200, content=data[query["source"], query["date"]], headers={
            "x-flow-normalized-key": ref["objectKey"], "x-flow-normalized-sha256": ref["sha256"]})
    status, ready = prepare(current, manifest, bundle, exchange=SimpleNamespace(request=request), headers={})
    assert status == {"status": "READY", "asOf": dates[-1]}
    expected_calls = 1 + 2 * len(recent["dates"]) - (len(recent["dates"]) - 1 if asymmetric else 0)
    assert len(calls) == expected_calls + int(asymmetric)
    new_calendar, new_wire, version = ready
    payloads, published = build_market_read_models(current, producer_build_sha="d" * 40,
        rolling_publication_input={"calendar": new_calendar, "checkpoint": new_wire,
            "checkpoint_sha256": sha256(new_wire).hexdigest(), "calculation_version": version})
    assert published["asOf"] == dates[-1]
    for row in payloads["radar"]["data"]["etfTerminal"]["instruments"]:
        assert row["periods"]["2w"] == 10 and row["periods"]["1m"] == 21
    # The next scheduler window may have the same real data date. Reuse the
    # newly bound current-day checkpoint instead of rejecting it as "future".
    active.update(wire=new_wire, asOf=dates[-1])
    status, again = prepare(current, manifest, bundle, exchange=SimpleNamespace(request=request), headers={})
    assert status["status"] == "READY" and sha256(again[1]).hexdigest() == sha256(new_wire).hexdigest()
    assert len(calls) == 2 * expected_calls + int(asymmetric)
    # A recurring opt-in must cold-start only once; with an active checkpoint
    # the same flag follows the ordinary continuation path.
    status_bootstrap, bootstrapped_again = prepare(current, manifest, bundle,
        exchange=SimpleNamespace(request=request), headers={}, bootstrap=True)
    assert status_bootstrap["status"] == "READY"
    assert sha256(bootstrapped_again[1]).hexdigest() == sha256(new_wire).hexdigest()
    assert len(calls) == 3 * expected_calls + int(asymmetric)
    # A later indexed revision cannot silently mix with the unchanged source
    # bundle from which the one-day snapshot was computed.
    changed = data["sse", dates[-1]].replace(b"2300000000", b"2400000000")
    assert changed != data["sse", dates[-1]]
    data["sse", dates[-1]] = changed
    digest = sha256(changed).hexdigest()
    ref = pinned["sse", dates[-1]]
    raw_sha = sha256(f"raw:sse:{dates[-1]}".encode()).hexdigest()
    ref.update(sha256=digest, objectKey=f"market-data/normalized/sse/etf_scale/{dates[-1]}/{sha256(f'{raw_sha}:{digest}'.encode()).hexdigest()}/data.csv")
    with pytest.raises(ValueError, match="collected_source_revision_changed"):
        prepare(current, manifest, bundle, exchange=SimpleNamespace(request=request), headers={})


def test_binding_requires_exact_active_dashboard_and_successful_activation():
    day = "2026-10-30"
    release = f"release:{day}:" + "a" * 16
    manifest = {"releaseId": release, "asOf": day, "slot": "dashboard", "manifestSha256": "b" * 64}
    receipt = {"readModelReleaseId": release, "asOf": day, "readModelActivation": "activated"}
    assert resolve_rolling_binding(receipt, manifest).endswith(f"/{release}/rolling-checkpoint/" + "b" * 64 + ".json")
    direct = {"releaseId": release, "asOf": day, "slot": "dashboard", "status": "activated"}
    assert resolve_rolling_binding(direct, manifest) == resolve_rolling_binding(receipt, manifest)
    with pytest.raises(ValueError, match="publication_binding_mismatch"):
        resolve_rolling_binding({**direct, "status": "published_historical"}, manifest)
    for status in ("staged_older_than_active", "staged_stale_revision", "unavailable", None):
        with pytest.raises(ValueError, match="publication_binding_mismatch"):
            resolve_rolling_binding({**receipt, "readModelActivation": status}, manifest)
    with pytest.raises(ValueError, match="publication_binding_mismatch"):
        resolve_rolling_binding(receipt, {**manifest, "releaseId": f"release:{day}:" + "c" * 16})


@pytest.mark.parametrize("status", [404, 503, 200])
def test_active_checkpoint_transport_fails_closed(status):
    from types import SimpleNamespace
    from trading_desk.pipeline_entry import read_active_rolling_checkpoint
    from trading_desk.pipeline_runner import PipelineRunnerError

    calls = []

    def request(method, url, **kwargs):
        assert method == "GET" and url.endswith("/etf-state")
        assert kwargs["params"] == {"kind": "rolling-checkpoint"}
        calls.append(url)
        return SimpleNamespace(status=status, content=b"unverified", headers={})

    def run():
        return read_active_rolling_checkpoint(exchange=SimpleNamespace(request=request), headers={})

    if status == 404:
        assert run() is None
    else:
        with pytest.raises(PipelineRunnerError, match="entry_rolling_checkpoint_"):
            run()
    assert len(calls) == 1


def test_checkpoint_rejects_mismatched_components_before_emitting_wire():
    refs = canonical_json_bytes([])
    cache = encode_daily_cache({})
    manifest = {"version": 1, "refsSha256": sha256(refs).hexdigest(),
                "cacheSha256": sha256(cache).hexdigest()}
    wire = pack_rolling_checkpoint(manifest, refs, cache)
    digest = sha256(wire).hexdigest()
    assert unpack_rolling_checkpoint(wire, digest) == (manifest, refs, cache)
    with pytest.raises(ValueError, match="checkpoint_mismatch"):
        pack_rolling_checkpoint(manifest, b"[1]", cache)
    with pytest.raises(ValueError, match="object_identity_invalid"):
        unpack_rolling_checkpoint(wire + b"x", digest)


def test_checkpoint_wire_expansion_and_node_count_are_hard_bounded():
    import gzip
    from trading_desk.etf_rolling_refs import MAX_DAILY_CACHE_BYTES, MAX_ROLLING_OBJECT_BYTES
    bomb = gzip.compress(b" " * (MAX_DAILY_CACHE_BYTES + 1), mtime=0)
    with pytest.raises(ValueError, match="cache_limit"):
        unpack_rolling_checkpoint(bomb, sha256(bomb).hexdigest())
    oversized = b"x" * (MAX_ROLLING_OBJECT_BYTES + 1)
    with pytest.raises(ValueError, match="object_identity_invalid"):
        unpack_rolling_checkpoint(oversized, sha256(oversized).hexdigest())
    nodes = {("sse", (date(2026, 1, 1) + timedelta(days=i)).isoformat()): {} for i in range(43)}
    with pytest.raises(ValueError, match="cache_limit"):
        encode_daily_cache(nodes)
    with pytest.raises(ValueError, match="cache_invalid"):
        decode_daily_cache(canonical_json_bytes({"version": 1, "nodes": [[s, d, n] for (s, d), n in nodes.items()]}))


def test_period_windows_roll_without_cash_or_compressing_missing_sessions():
    start = date(2026, 10, 8)
    dates = [
        d.isoformat() for i in range(40)
        if (d := start + timedelta(days=i)).weekday() < 5
    ][:23]
    calendar = {
        "sourceId": "sse_official_trading_calendar",
        "sha256": "a" * 64,
        "dates": dates,
        "datesSha256": sha256(json.dumps(dates, separators=(",", ":")).encode()).hexdigest(),
    }

    def build(sessions):
        return materialize_etf_terminal({
            "dates": sessions,
            "officialCalendar": calendar,
            "instruments": [
                {"market": market, "code": code,
                 "values": [dates.index(d) + 1 for d in sessions],
                 "cashValues": [None] * len(sessions)}
                for market, code in [("SSE", "510300"), ("SZSE", "159919")]
            ],
        })

    before, after = build(dates[:-1]), build(dates)
    active = {"slot": "dashboard", "asOf": dates[-2],
              "releaseId": "release:fixture", "manifestSha256": "a" * 64}
    ref = {**active, "sha256": "b" * 64}
    assert validate_published_checkpoint_ref(ref, active, calendar, dates[-1]) == "b" * 64
    for mismatch in ({"asOf": dates[-3]}, {"slot": "etf_penetration"},
                     {"releaseId": "release:other"}, {"manifestSha256": "c" * 64}):
        with pytest.raises(ValueError, match="published_checkpoint_mismatch"):
            validate_published_checkpoint_ref(ref, {**active, **mismatch}, calendar, dates[-1])
    for period, count in [("2w", 10), ("1m", 21)]:
        for result, sessions in [(before, dates[:-1]), (after, dates)]:
            proof = result["windowEvidence"]["periods"][period]
            assert proof["status"] == "READY"
            assert proof["baselineDate"] == sessions[-count - 1]
            assert proof["dates"] == sessions[-count:]
            for row in result["instruments"]:
                assert row["periods"][period] == sum(dates.index(d) + 1 for d in sessions[-count:])
                assert row["estimatedCashPeriods"][period] is None
    gap = build(dates[:5] + dates[6:])
    assert gap["windowEvidence"]["periods"]["2w"]["status"] == "READY"
    assert gap["windowEvidence"]["periods"]["1m"]["status"] == "MISSING"

    refs = [{"sourceId": market, "tradeDate": day}
            for day in dates for market in ("sse", "szse")]
    previous, missing = select_rolling_refs(calendar, dates[-2], refs)
    assert not missing and previous == refs[:44]
    current, missing = select_rolling_refs(calendar, dates[-1], refs[:-1])
    assert current == previous[2:] + refs[-2:-1]
    assert missing == [("szse", dates[-1])]

    calls = []

    def lookup(slots):
        calls.append(slots)
        return refs[-2:]

    resolved, missing = resolve_rolling_refs(calendar, dates[-1], previous, lookup)
    assert resolved == refs[2:] and not missing
    assert calls == [[{"source": market, "date": dates[-1]} for market in ("sse", "szse")]]
    assert len(resolve_rolling_refs(calendar, dates[-1], [], lookup)[1]) == 44
    assert len(calls) == 1

    available = [(r["sourceId"], r["tradeDate"]) for r in previous[2:]]
    plan = plan_daily_reuse(calendar, dates[-1], resolved, previous, available)
    assert len(plan["reuse"]) == 40
    assert plan["rebuild"] == [("sse", dates[-1]), ("szse", dates[-1])]
    assert len(plan["reads"]) == 4

    revised = [{**r, "revision": 2}
               if r["sourceId"] == "sse" and r["tradeDate"] == dates[8] else r
               for r in resolved]
    plan = plan_daily_reuse(calendar, dates[-1], revised, previous, available)
    assert plan["rebuild"][:2] == [("sse", dates[8]), ("sse", dates[9])]
    assert len(plan["reads"]) == 7

    cold = plan_daily_reuse(calendar, dates[-1], resolved, [], [])
    assert cold["status"] == "BOUNDED_RECOVERY_REQUIRED"
    assert cold["requiredReadCount"] == 44 and cold["reads"] == []
    bootstrap_plan = plan_daily_reuse(calendar, dates[-1], resolved, [], [], max_archive_reads=44)
    assert bootstrap_plan["status"] == "WITHIN_READ_LIMIT" and len(bootstrap_plan["reads"]) == 44
    with pytest.raises(ValueError, match="archive_read_limit_invalid"):
        plan_daily_reuse(calendar, dates[-1], resolved, [], [], max_archive_reads=45)

    def payload(ref):
        return ref["tradeDate"].encode()

    pinned = [{**r, "sha256": sha256(payload(r)).hexdigest()} for r in refs]
    by_key = {(r["sourceId"], r["tradeDate"]): r for r in pinned}
    daily = {(m, d): {
        "inputSha256": canonical_sha256(["fixture-v1", by_key[m, dates[dates.index(d)-1]], by_key[m, d]]),
        "valueSha256": canonical_sha256("cached"), "value": "cached",
    } for m, d in available}
    reads = []

    def read(ref):
        reads.append(ref)
        return payload(ref)

    def rebuild(source, prev, day, left, right):
        assert left == prev.encode() and right == day.encode()
        return source

    daily = decode_daily_cache(encode_daily_cache(daily))
    _, result = execute_daily_reuse(calendar, dates[-1], pinned, pinned[:-2], daily,
                                    read=read, build=rebuild, calculation_version="fixture-v1")
    assert len(reads) == 4 and len(result) == 42
    assert result["szse", dates[-1]]["value"] == "szse"
    assert result["sse", dates[-2]]["value"] == "cached"
    _, result = execute_daily_reuse(calendar, dates[-1], pinned, [], {},
                                    read=read, build=rebuild, calculation_version="fixture-v1")
    assert result is None and len(reads) == 4
    _, result = execute_daily_reuse(calendar, dates[-1], pinned, [], {},
                                    read=read, build=rebuild, calculation_version="fixture-v1",
                                    max_archive_reads=44)
    assert len(reads) == 48 and len(result) == 42
    daily["sse", dates[-2]]["value"] = "tampered"
    plan, result = execute_daily_reuse(calendar, dates[-1], pinned, pinned[:-2], daily,
                                      read=read, build=rebuild, calculation_version="fixture-v1")
    assert ("sse", dates[-2]) in plan["rebuild"]
    assert result["sse", dates[-2]]["value"] == "sse"
    read_count = len(reads)
    plan, result = execute_daily_reuse(calendar, dates[-1], pinned, pinned[:-2], daily,
                                      read=read, build=rebuild, calculation_version="fixture-v2")
    assert plan["status"] == "BOUNDED_RECOVERY_REQUIRED"
    assert result is None and len(reads) == read_count

    nodes = {(source, day): {"value": {"instruments": [{
        "market": source.upper(), "code": code, "periods": {"1d": 1},
    }]}} for day in dates[1:] for source, code in [("sse", "510300"), ("szse", "159919")]}
    terminal = materialize_rolling_daily(calendar, dates[-1], nodes)
    assert len(terminal["instruments"]) == 2
    for row in terminal["instruments"]:
        assert row["periods"]["2w"] == 10
        assert row["periods"]["1m"] == 21
        assert row["estimatedCashPeriods"]["1m"] is None

    bad = {**pinned[-1], "objectKey":
           f"market-data/normalized/szse/etf_scale/{dates[-2]}/{'a' * 64}/data.csv"}
    with pytest.raises(ValueError, match="etf_rolling_archive_identity_invalid"):
        build_rolling_candidate(
            {"asOf": dates[-1], "publication": {"status": "confirmed"}},
            calendar, [bad], [], {},
            read=lambda ref: pytest.fail("invalid identity must not read archive"),
            calculation_version="fixture-v1",
        )


@pytest.mark.parametrize("asymmetric", [False, True])
def test_initial_rolling_checkpoint_bootstrap_uses_official_dates_and_exact_archives(asymmetric):
    import base64
    from types import SimpleNamespace
    from scripts.prepare_rolling_etf_publication import (
        BOOTSTRAP_CALENDAR_LOOKBACK_DAYS,
        SOURCE_PROXY_URL,
        prepare,
    )
    from trading_desk.etf_rolling_refs import MAX_INITIAL_ROLLING_ARCHIVE_READS
    from trading_desk.etf_period_dependencies import OFFICIAL_MARGIN_DATES_SOURCE
    from trading_desk.etf_replay import REPLAY_CONTRACT
    from trading_desk.read_models import build_market_read_models

    snapshot, _ = rolling_production_fixture()
    target = snapshot["asOf"]
    end = date.fromisoformat(target)
    start = (end - timedelta(days=BOOTSTRAP_CALENDAR_LOOKBACK_DAYS)).isoformat()
    start_date = date.fromisoformat(start)
    official_dates = [(start_date + timedelta(days=offset)).isoformat()
        for offset in range(BOOTSTRAP_CALENDAR_LOOKBACK_DAYS + 1)
        if (start_date + timedelta(days=offset)).weekday() < 5]
    official_raw = canonical_json_bytes({"beginDate": start.replace("-", ""),
        "endDate": target.replace("-", ""),
        "result": [{"opDate": day.replace("-", "")} for day in official_dates],
        "pageHelp": {"pageCount": 1, "total": len(official_dates)}})
    dates = rolling_production_fixture()[1]["calendar"]["dates"]
    current_dates = dates[-3:]
    current_raw = canonical_json_bytes({"beginDate": current_dates[0].replace("-", ""),
        "endDate": current_dates[-1].replace("-", ""),
        "result": [{"opDate": day.replace("-", "")} for day in current_dates],
        "pageHelp": {"pageCount": 1, "total": len(current_dates)}})
    run_id = "bootstrap-fixture"
    manifest = {"run_id": run_id, "requested_dates": current_dates, "artifacts": []}
    bundle = {"version": 1, "history": {"runId": run_id, "asOf": target, "artifacts": []}}
    calendar_sha = sha256(current_raw).hexdigest()
    manifest["artifacts"].append({"source": {"source_id": "sse", "dataset": "margin_market_daily"},
        "requested_date": f"{current_dates[0]}_{current_dates[-1]}", "sha256": calendar_sha})
    bundle["history"]["artifacts"].append({"sourceId": "sse", "dataset": "margin_market_daily",
        "requestedDate": f"{current_dates[0]}_{current_dates[-1]}", "sha256": calendar_sha,
        "rawContentBase64": base64.b64encode(current_raw).decode()})

    sources = {}
    for index, day in enumerate(dates):
        for source, market, code in (("sse", "SSE", "510300"), ("szse", "SZSE", "159915")):
            raw = f"official:{source}:{day}".encode()
            raw_sha = sha256(raw).hexdigest()
            content = ("trade_date,market,instrument_code,shares\n"
                f"{day},{market},{code},{index * 100000000}\n").encode()
            digest = sha256(content).hexdigest()
            revision = sha256(f"{raw_sha}:{digest}".encode()).hexdigest()
            key = f"market-data/normalized/{source}/etf_scale/{day}/{revision}/data.csv"
            sources[source, day] = (raw_sha, digest, key, content)
            if day in current_dates and (not asymmetric or source == "sse" or day == target):
                manifest["artifacts"].append({"source": {"source_id": source, "dataset": "etf_scale"},
                    "requested_date": day, "sha256": raw_sha})
                bundle["history"]["artifacts"].append({"sourceId": source, "dataset": "etf_scale",
                    "requestedDate": day, "sha256": raw_sha,
                    "rawContentBase64": base64.b64encode(raw).decode(),
                    "normalized": [{"name": "data", "tradeDates": [day], "sha256": digest,
                        "contentBase64": base64.b64encode(content).decode()}]})

    class ArchiveExchange:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            assert method == "GET"
            params = kwargs.get("params", {})
            self.calls.append((url, dict(params)))
            if url.endswith("/etf-state"):
                return SimpleNamespace(status=404, content=b"", headers={})
            assert url.endswith("/history-artifact")
            source, day = params["source"], params["date"]
            assert params.get("indexed") == "1" or params.get("selection") == "indexed-or-single"
            raw_sha, digest, key, content = sources[source, day]
            headers = {"X-Flow-Normalized-Key": key, "X-Flow-Normalized-Sha256": digest,
                "X-Flow-Trade-Date": day}
            if params.get("indexed") == "1":
                headers["X-Flow-Pcf-Revision"] = key.split("/")[-2]
            else:
                headers.update({"X-Flow-Normalized-Candidate-Count": "1",
                    "X-Flow-Normalized-Selection": "single"})
            return SimpleNamespace(status=200, content=content, headers=headers)

    class CalendarExchange:
        def request(self, method, url, **kwargs):
            assert method == "GET" and url.endswith("queryMargin.do")
            assert kwargs["params"]["beginDate"] == start.replace("-", "")
            assert kwargs["params"]["endDate"] == target.replace("-", "")
            return SimpleNamespace(status=200, content=official_raw, headers={})

    archive = ArchiveExchange()
    status, ready = prepare(snapshot, manifest, bundle, exchange=archive, headers={}, bootstrap=True,
        source_exchange=CalendarExchange(), source_proxy_url=SOURCE_PROXY_URL)
    assert status == {"status": "READY", "asOf": target, "bootstrap": True,
        "archiveReadRequests": MAX_INITIAL_ROLLING_ARCHIVE_READS,
        "calendarSource": OFFICIAL_MARGIN_DATES_SOURCE}
    archive_reads = [params for url, params in archive.calls if url.endswith("/history-artifact")]
    assert len(archive_reads) == MAX_INITIAL_ROLLING_ARCHIVE_READS
    expected_indexed = 4 if asymmetric else 6
    assert len([params for params in archive_reads if params.get("indexed") == "1"]) == expected_indexed
    assert len([params for params in archive_reads if params.get("indexed") != "1"]) == 44 - expected_indexed
    if asymmetric:
        assert all(p.get("indexed") != "1" for p in archive_reads
            if p["source"] == "szse" and p["date"] in current_dates[:-1])
    calendar, wire, version = ready
    assert version == REPLAY_CONTRACT and target in calendar["dates"]
    payloads, release = build_market_read_models(snapshot, producer_build_sha="d" * 40,
        rolling_publication_input={"calendar": calendar, "checkpoint": wire,
            "checkpoint_sha256": sha256(wire).hexdigest(), "calculation_version": version})
    assert release["rollingPublication"]["status"] == "READY"
    rows = payloads["radar"]["data"]["etfTerminal"]["instruments"]
    assert len(rows) == 2 and all(row["periods"]["2w"] is not None and row["periods"]["1m"] is not None for row in rows)
    assert all(row["estimatedCashPeriods"].get("1m") is None for row in rows)

    # A declaration on either side without matching proof must never turn
    # into a historical archive fallback, even if that archive is available.
    for entries in (manifest["artifacts"], bundle["history"]["artifacts"]):
        entry = entries.pop()
        with pytest.raises(ValueError, match="rolling_collected_source_identity_missing"):
            prepare(snapshot, manifest, bundle, exchange=ArchiveExchange(), headers={}, bootstrap=True,
                source_exchange=CalendarExchange(), source_proxy_url=SOURCE_PROXY_URL)
        entries.append(entry)
    manifest["artifacts"].pop()
    bundle["history"]["artifacts"].pop()
    with pytest.raises(ValueError, match="rolling_collected_target_not_observed"):
        prepare(snapshot, manifest, bundle, exchange=ArchiveExchange(), headers={}, bootstrap=True,
            source_exchange=CalendarExchange(), source_proxy_url=SOURCE_PROXY_URL)


def test_bootstrap_archive_requires_single_candidate_or_unique_pointer():
    from types import SimpleNamespace
    from scripts.prepare_rolling_etf_publication import _read_bootstrap_archive
    day, source = "2026-11-04", "sse"
    content = b"trade_date,market,instrument_code,shares\n2026-11-04,SSE,510300,1\n"
    digest = sha256(content).hexdigest()
    key = f"market-data/normalized/{source}/etf_scale/{day}/{'a' * 64}/data.csv"
    def request(headers):
        return SimpleNamespace(status=200, content=content, headers={
            "X-Flow-Normalized-Key": key, "X-Flow-Normalized-Sha256": digest,
            "X-Flow-Trade-Date": day, **headers})
    calls = []
    def indexed_request(*args, **kwargs):
        calls.append(kwargs["params"])
        return request({"X-Flow-Pcf-Revision": "a" * 64})
    ref, actual = _read_bootstrap_archive(source, day,
        exchange=SimpleNamespace(request=indexed_request), headers={})
    assert actual == content and ref["objectKey"] == key and ref["sha256"] == digest
    assert len(calls) == 1 and calls[0]["selection"] == "indexed-or-single"
    for response in (
        SimpleNamespace(status=404, content=b"", headers={}),
        SimpleNamespace(status=200, content=b"tampered", headers=request({"X-Flow-Pcf-Revision": "a" * 64}).headers),
        request({"X-Flow-Pcf-Revision": "b" * 64}),
    ):
        with pytest.raises(ValueError, match="identity_invalid|selection_unproven"):
            _read_bootstrap_archive(source, day,
                exchange=SimpleNamespace(request=lambda *a, **k: response), headers={})
    with pytest.raises(ValueError, match="archive_ambiguous"):
        _read_bootstrap_archive(source, day,
            exchange=SimpleNamespace(request=lambda *a, **k: request({
                "X-Flow-Normalized-Candidate-Count": "2", "X-Flow-Normalized-Selection": "latest-uploaded"})),
            headers={})
    with pytest.raises(ValueError, match="selection_unproven"):
        _read_bootstrap_archive(source, day,
            exchange=SimpleNamespace(request=lambda *a, **k: request({})), headers={})


def test_bootstrap_calendar_relay_is_single_bounded_fallback_and_rejects_bad_direct_evidence():
    from types import SimpleNamespace
    from scripts.prepare_rolling_etf_publication import (
        BOOTSTRAP_CALENDAR_LOOKBACK_DAYS,
        SOURCE_PROXY_URL,
        _calendar_from_official_response,
        _fetch_bootstrap_calendar,
    )
    from trading_desk.etf_period_dependencies import OFFICIAL_MARGIN_DATES_SOURCE

    target = "2026-08-05"
    end = date.fromisoformat(target)
    start = (end - timedelta(days=BOOTSTRAP_CALENDAR_LOOKBACK_DAYS)).isoformat()
    start_date = date.fromisoformat(start)
    official_dates = [(start_date + timedelta(days=offset)).isoformat()
        for offset in range(BOOTSTRAP_CALENDAR_LOOKBACK_DAYS + 1)
        if (start_date + timedelta(days=offset)).weekday() < 5
        and (start_date + timedelta(days=offset)).isoformat() not in {"2026-05-01", "2026-05-04", "2026-05-05"}]
    raw = canonical_json_bytes({"beginDate": start.replace("-", ""), "endDate": target.replace("-", ""),
        "result": [{"opDate": day.replace("-", "")} for day in official_dates],
        "pageHelp": {"pageCount": 1, "total": len(official_dates)}})

    class WorkerExchange:
        def __init__(self):
            self.calls, self.attempts, self.bytes = [], 0, 0
        def check_deadline(self): pass
        def record_source_attempt(self): self.attempts += 1
        def record_external_response(self, size): self.bytes += size
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            self.bytes += len(raw)
            return SimpleNamespace(status=200, content=raw, headers={})

    worker = WorkerExchange()
    direct = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(status=503, content=b"", headers={}))
    calendar = _fetch_bootstrap_calendar(target, source_exchange=direct, exchange=worker,
        headers={"Authorization": "Bearer fixture"}, source_proxy_url=SOURCE_PROXY_URL)
    assert calendar["sourceId"] == OFFICIAL_MARGIN_DATES_SOURCE
    assert calendar["dates"][-1] == target and len(calendar["dates"]) >= 22
    assert worker.attempts == 2 and len(worker.calls) == 1
    method, url, kwargs = worker.calls[0]
    assert method == "GET" and url == SOURCE_PROXY_URL
    assert kwargs["params"]["path"].endswith("queryMargin.do")
    assert worker.bytes == len(raw)

    boolean_page_count = json.loads(raw)
    boolean_page_count["pageHelp"]["pageCount"] = True
    with pytest.raises(ValueError, match="calendar_invalid"):
        _calendar_from_official_response(canonical_json_bytes(boolean_page_count),
            start=start, end=target, target=target)

    # Malformed successful evidence is not retried; client/configuration
    # errors do not spend the one relay fallback either.
    malformed_worker = WorkerExchange()
    malformed_direct = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(
        status=200, content=b"{}", headers={}))
    with pytest.raises(ValueError, match="calendar_invalid"):
        _fetch_bootstrap_calendar(target, source_exchange=malformed_direct, exchange=malformed_worker,
            headers={}, source_proxy_url=SOURCE_PROXY_URL)
    assert malformed_worker.calls == [] and malformed_worker.attempts == 1

    client_error_worker = WorkerExchange()
    client_error_direct = SimpleNamespace(request=lambda *args, **kwargs: SimpleNamespace(
        status=400, content=b"bad request", headers={}))
    with pytest.raises(ValueError, match="calendar_unavailable"):
        _fetch_bootstrap_calendar(target, source_exchange=client_error_direct, exchange=client_error_worker,
            headers={}, source_proxy_url=SOURCE_PROXY_URL)
    assert client_error_worker.calls == [] and client_error_worker.attempts == 1


def test_requested_bootstrap_unavailability_does_not_block_confirmed_daily_publication(tmp_path, monkeypatch, capsys):
    import sys
    import scripts.prepare_rolling_etf_publication as entry
    snapshot, _ = rolling_production_fixture()
    source = canonical_json_bytes(snapshot)
    (tmp_path / "snapshot.json").write_bytes(source)
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "history.json").write_text("{}")
    report = tmp_path / "status.json"
    monkeypatch.setattr(sys, "argv", ["prepare", "--snapshot", str(tmp_path / "snapshot.json"),
        "--manifest", str(tmp_path / "manifest.json"), "--history-bundle", str(tmp_path / "history.json"),
        "--output", str(tmp_path / "ready"), "--report", str(report), "--bootstrap"])
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setattr(entry, "prepare", lambda *args, **kwargs: (
        {"status": "UNAVAILABLE", "reason": "rolling_bootstrap_calendar_unavailable"}, None))

    entry.main()

    status = json.loads(report.read_bytes())
    assert status["status"] == "UNAVAILABLE"
    assert status["reason"] == "rolling_bootstrap_calendar_unavailable"
    assert not (tmp_path / "ready").exists()
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_bootstrap_workflow_is_opt_in_and_runs_before_any_publish_dispatch():
    from pathlib import Path
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/market-data-collect.yml").read_text()
    start = workflow.index("      rolling_bootstrap:")
    end = workflow.index("\n\n# Cloudflare Cron", start)
    input_block = workflow[start:end]
    assert "default: false" in input_block and "type: boolean" in input_block
    assert 'description: "Build rolling proof in the reconcile window' in input_block
    assert "inputs.recovery_id == ''" in workflow
    assert "rolling_bootstrap_checkpoint_already_exists" not in Path(
        __file__).resolve().parents[2].joinpath("scripts/prepare_rolling_etf_publication.py").read_text()
    gate_start = workflow.index("- name: Claim or skip pipeline window")
    claim = workflow.index("/claim", gate_start)
    validation = workflow.index("Rolling bootstrap requires the rolling flag", gate_start)
    assert validation < claim
    assert '"$WINDOW" == "reconcile"' in workflow
    assert 'Rolling checkpoint preparation is restricted to the reconcile window.' in workflow
    preparation = workflow.index("- name: Prepare rolling proof from the active published checkpoint")
    publish_dispatch = workflow.index("- name: Dispatch resumable market-data publish workflow", preparation)
    block = workflow[preparation:publish_dispatch]
    assert 'BOOTSTRAP_ARGS+=(--bootstrap)' in block
    assert 'INPUT_ROLLING_BOOTSTRAP' in block
    assert "needs.pipeline_window_gate.outputs.window_name == 'reconcile'" in block
    assert "FLOW_SOURCE_PROXY_URL: https://flow.xiaoker.com/api/v1/admin/source-proxy" in block
