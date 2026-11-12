"""Read-only, bounded continuation or explicit first rolling-checkpoint bootstrap.

Does not recollect ETF share source data; it fetches one bounded official SSE
business-date proof. It performs no dispatch, claim, pointer mutation or write.
Initial bootstrap is an explicit manual opt-in in the existing Collector.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time

from trading_desk.etf_rolling_refs import (build_from_published_checkpoint,
    unpack_rolling_checkpoint, encode_daily_cache, pack_rolling_checkpoint, select_rolling_refs,
    build_rolling_candidate, MAX_INITIAL_ROLLING_ARCHIVE_READS)
from trading_desk.etf_period_dependencies import (compose_official_margin_calendars,
    validate_official_calendar, OFFICIAL_MARGIN_DATES_SOURCE, COMPOSITE_MARGIN_DATES_SOURCE,
    MAX_CALENDAR_DATES)
from trading_desk.etf_replay import REPLAY_CONTRACT
from trading_desk.pipeline_application import canonical_sha256
from trading_desk.phase0_archive_recipe import official_calendar_from_bundle
from trading_desk.pipeline_entry import read_active_rolling_checkpoint, read_exact_etf_archive, candidate_credentials
from trading_desk.read_models import canonical_json_bytes
from scripts.build_rolling_etf_candidate import write_source_bundle, read_bounded


NORMAL_REQUEST_LIMIT = 23
BOOTSTRAP_WORKER_REQUEST_LIMIT = 46  # checkpoint + 44 archive reads + one SSE relay fallback
NORMAL_RESPONSE_BYTE_LIMIT = 4_000_000
BOOTSTRAP_RESPONSE_BYTE_LIMIT = 32_000_000  # 4MB * 44 / 6, rounded up with bounded overhead
BOOTSTRAP_CALENDAR_LOOKBACK_DAYS = 120
BOOTSTRAP_SOURCE_MAX_BYTES = 2_000_000
SOURCE_PROXY_URL = "https://flow.xiaoker.com/api/v1/admin/source-proxy"
SSE_MARGIN_URL = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
SSE_MARGIN_PATH = "/marketdata/tradedata/queryMargin.do"


def _calendar_from_official_response(raw, *, start, end, target):
    if not isinstance(raw, bytes) or not raw or len(raw) > BOOTSTRAP_SOURCE_MAX_BYTES:
        raise ValueError("rolling_bootstrap_calendar_size_invalid")
    try:
        response = json.loads(raw)
        rows = response["result"]
        page = response["pageHelp"]
        begin, actual_end = response["beginDate"], response["endDate"]
        if (not isinstance(rows, list) or not rows or len(rows) > MAX_CALENDAR_DATES
                or not isinstance(page, dict) or type(page.get("pageCount")) is not int
                or type(page.get("total")) is not int):
            raise ValueError()
        dates = []
        for row in rows:
            op_date = str(row["opDate"])
            if not re.fullmatch(r"[0-9]{8}", op_date):
                raise ValueError()
            dates.append(f"{op_date[:4]}-{op_date[4:6]}-{op_date[6:]}")
        dates.sort()
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise ValueError("rolling_bootstrap_calendar_invalid") from None
    if (page.get("pageCount") != 1
            or page.get("total") != len(rows)
            or begin != start.replace("-", "") or actual_end != end.replace("-", "")
            or dates != sorted(set(dates)) or dates[-1] != target or target not in dates
            or dates.index(target) < 21):
        raise ValueError("rolling_bootstrap_calendar_incomplete")
    calendar = {"sourceId": OFFICIAL_MARGIN_DATES_SOURCE, "sha256": sha256(raw).hexdigest(),
        "dates": dates, "datesSha256": canonical_sha256(dates), "evidence": {
            "contentBase64": base64.b64encode(raw).decode(), "rangeStart": start, "rangeEnd": end}}
    validate_official_calendar(calendar)
    return calendar


def _fetch_bootstrap_calendar(target, *, source_exchange, exchange, headers, source_proxy_url):
    end = date.fromisoformat(target)
    start = (end - timedelta(days=BOOTSTRAP_CALENDAR_LOOKBACK_DAYS)).isoformat()
    params = {"isPagination": "true", "beginDate": start.replace("-", ""),
        "endDate": target.replace("-", ""), "tabType": "", "stockCode": "",
        "pageHelp.pageSize": "5000", "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1", "pageHelp.endPage": "5"}
    request_headers = {"Referer": "https://www.sse.com.cn/market/othersdata/margin/sum/"}
    if hasattr(exchange, "check_deadline"):
        exchange.check_deadline()
    if hasattr(exchange, "record_source_attempt"):
        exchange.record_source_attempt()
    direct_error = None
    try:
        reply = source_exchange.request("GET", SSE_MARGIN_URL, max_bytes=BOOTSTRAP_SOURCE_MAX_BYTES,
            chunk_size=65536, max_chunks=33, deadline_seconds=20,
            headers=request_headers, params=dict(params))
    except Exception as error:
        direct_error = error
    if direct_error is not None and str(direct_error) in {
            "response_limit", "response_deadline", "response_size_limit"}:
        raise ValueError("rolling_bootstrap_calendar_read_limit") from None
    if direct_error is None and hasattr(exchange, "record_external_response"):
        exchange.record_external_response(len(reply.content))
    if direct_error is None and reply.status == 200:
        return _calendar_from_official_response(reply.content, start=start, end=target, target=target)
    if direct_error is None and reply.status in {413, 422}:
        raise ValueError("rolling_bootstrap_calendar_read_limit")
    if direct_error is None and reply.status not in {401, 403, 408, 425, 429, 500, 502, 503, 504,
            520, 521, 522, 523, 524}:
        raise ValueError("rolling_bootstrap_calendar_unavailable")
    # Only a transport or upstream status failure may use the one approved
    # relay attempt; malformed or oversized official evidence is never retried.
    if source_proxy_url != SOURCE_PROXY_URL:
        raise ValueError("rolling_bootstrap_calendar_unavailable")
    if hasattr(exchange, "record_source_attempt"):
        exchange.record_source_attempt()
    proxy_params = {"path": SSE_MARGIN_PATH, **params}
    reply = exchange.request("GET", source_proxy_url, headers=headers, params=proxy_params,
        max_bytes=BOOTSTRAP_SOURCE_MAX_BYTES, chunk_size=65536, max_chunks=33, deadline_seconds=20)
    if reply.status != 200:
        raise ValueError("rolling_bootstrap_calendar_unavailable")
    return _calendar_from_official_response(reply.content, start=start, end=target, target=target)


def _collected_source_identity(source, day, manifest, history_bundle, *, allow_absent=False):
    declared = [item for item in manifest["artifacts"]
        if item.get("requested_date") == day
        and item.get("source", {}).get("source_id") == source
        and item.get("source", {}).get("dataset") == "etf_scale"]
    artifacts = [artifact for artifact in history_bundle["history"]["artifacts"]
        if artifact.get("sourceId") == source and artifact.get("dataset") == "etf_scale"
        and artifact.get("requestedDate") == day]
    if not declared and not artifacts:
        if allow_absent:
            return None
        raise ValueError("rolling_collected_target_not_observed")
    matches = [(artifact, part) for artifact in artifacts
        for part in artifact.get("normalized", [])
        if part.get("name") == "data" and part.get("tradeDates") == [day]]
    if len(declared) != 1 or len(artifacts) != 1 or len(matches) != 1:
        raise ValueError("rolling_collected_source_identity_missing")
    artifact, part = matches[0]
    try:
        if any(not isinstance(encoded, str) or len(encoded) > 2_800_000
                for encoded in (artifact["rawContentBase64"], part["contentBase64"])):
            raise ValueError("invalid_source_bytes")
        raw = base64.b64decode(artifact["rawContentBase64"], validate=True)
        normalized = base64.b64decode(part["contentBase64"], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ValueError("rolling_collected_source_identity_invalid") from None
    if (len(raw) > BOOTSTRAP_SOURCE_MAX_BYTES or len(normalized) > BOOTSTRAP_SOURCE_MAX_BYTES
            or sha256(raw).hexdigest() != artifact["sha256"]
            or sha256(normalized).hexdigest() != part["sha256"]
            or declared[0].get("sha256") != artifact["sha256"]):
        raise ValueError("rolling_collected_source_identity_invalid")
    return artifact["sha256"], part["sha256"]


def _read_bootstrap_archive(source, day, *, exchange, headers):
    reply = exchange.request("GET", "https://flow.xiaoker.com/api/v1/admin/history-artifact",
        headers=headers, params={"source": source, "dataset": "etf_scale", "date": day, "file": "data",
            "selection": "indexed-or-single"},
        max_bytes=BOOTSTRAP_SOURCE_MAX_BYTES, chunk_size=65536, max_chunks=33, deadline_seconds=20)
    values = {key.lower(): value for key, value in reply.headers.items()}
    key = values.get("x-flow-normalized-key", "")
    digest = values.get("x-flow-normalized-sha256", "")
    candidate_count = values.get("x-flow-normalized-candidate-count")
    revision = values.get("x-flow-pcf-revision")
    if (reply.status != 200 or not re.fullmatch(
            rf"market-data/normalized/{source}/etf_scale/{day}/[a-f0-9]{{64}}/data\.csv", key)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or values.get("x-flow-trade-date") != day
            or sha256(reply.content).hexdigest() != digest):
        raise ValueError("rolling_bootstrap_archive_identity_invalid")
    if candidate_count is not None:
        if candidate_count != "1" or values.get("x-flow-normalized-selection") != "single":
            raise ValueError("rolling_bootstrap_archive_ambiguous")
    elif not re.fullmatch(r"[a-f0-9]{64}", revision or "") or key.split("/")[-2] != revision:
        # The indexed pointer is unique by source/date/file; an unindexed
        # listing must instead prove that it found exactly one immutable file.
        raise ValueError("rolling_bootstrap_archive_selection_unproven")
    return {"sourceId": source, "tradeDate": day, "objectKey": key, "sha256": digest}, reply.content


def _prepare_initial_checkpoint(snapshot, manifest, history_bundle, *, exchange,
                                source_exchange, headers, source_proxy_url):
    target = snapshot["asOf"]
    current_calendar = official_calendar_from_bundle(manifest=manifest,
        history_bundle=history_bundle, target_date=target)
    official_calendar = _fetch_bootstrap_calendar(target, source_exchange=source_exchange,
        exchange=exchange, headers=headers, source_proxy_url=source_proxy_url)
    calendar = extend_calendar(official_calendar, current_calendar, target)
    _, missing = select_rolling_refs(calendar, target, [])
    if len(missing) != MAX_INITIAL_ROLLING_ARCHIVE_READS:
        raise ValueError("rolling_bootstrap_window_invalid")
    current_dates = set(current_calendar["dates"])
    if not current_dates.issubset({day for _, day in missing}):
        raise ValueError("rolling_bootstrap_current_dates_outside_window")

    refs, archive_bytes = [], {}
    for source, day in missing:
        # Calendar coverage and collection coverage are independent. The two
        # markets need not have collected the same historical dates this run.
        identity = _collected_source_identity(source, day, manifest, history_bundle,
            allow_absent=day != target)
        if identity is not None:
            raw_sha, expected_sha = identity
            reply = exchange.request("GET", "https://flow.xiaoker.com/api/v1/admin/history-artifact",
                headers=headers, params={"source": source, "dataset": "etf_scale",
                    "date": day, "file": "data", "indexed": "1"},
                max_bytes=BOOTSTRAP_SOURCE_MAX_BYTES, chunk_size=65536, max_chunks=33, deadline_seconds=20)
            values = {key.lower(): value for key, value in reply.headers.items()}
            key, digest = values.get("x-flow-normalized-key"), values.get("x-flow-normalized-sha256")
            if (reply.status != 200 or not isinstance(key, str)
                    or not re.fullmatch(rf"market-data/normalized/{source}/etf_scale/{day}/[a-f0-9]{{64}}/data\.csv", key)
                    or values.get("x-flow-trade-date") != day
                    or sha256(reply.content).hexdigest() != digest):
                raise ValueError("rolling_indexed_source_identity_mismatch")
            if digest != expected_sha or key.split("/")[-2] not in {
                    raw_sha, sha256(f"{raw_sha}:{digest}".encode()).hexdigest()}:
                raise ValueError("rolling_collected_source_revision_changed")
            ref = {"sourceId": source, "tradeDate": day, "objectKey": key, "sha256": digest}
            content = reply.content
        else:
            ref, content = _read_bootstrap_archive(source, day, exchange=exchange, headers=headers)
        refs.append(ref)
        archive_bytes[source, day] = content

    from trading_desk.etf_rolling_refs import encode_daily_cache
    plan, candidate, nodes = build_rolling_candidate(snapshot, calendar, refs, [], {},
        read=lambda ref: archive_bytes[ref["sourceId"], ref["tradeDate"]],
        calculation_version=REPLAY_CONTRACT,
        max_archive_reads=MAX_INITIAL_ROLLING_ARCHIVE_READS)
    if (candidate is None or nodes is None or plan["status"] != "WITHIN_READ_LIMIT"
            or plan["requiredReadCount"] != MAX_INITIAL_ROLLING_ARCHIVE_READS):
        raise ValueError("rolling_bootstrap_builder_incomplete")
    selected, still_missing = select_rolling_refs(calendar, target, refs)
    if still_missing or len(selected) != MAX_INITIAL_ROLLING_ARCHIVE_READS:
        raise ValueError("rolling_bootstrap_archive_gap")
    ref_bytes, node_bytes = canonical_json_bytes(selected), encode_daily_cache(nodes)
    wire = pack_rolling_checkpoint({"version": 1, "asOf": target,
        "calculationVersion": REPLAY_CONTRACT, "officialCalendar": calendar,
        "refsSha256": sha256(ref_bytes).hexdigest(), "cacheSha256": sha256(node_bytes).hexdigest()},
        ref_bytes, node_bytes)
    return {"status": "READY", "asOf": target, "bootstrap": True,
        "archiveReadRequests": len(selected), "calendarSource": OFFICIAL_MARGIN_DATES_SOURCE}, (
            calendar, wire, REPLAY_CONTRACT)


def extend_calendar(previous, current, target):
    """Keep a minimal overlapping raw-proof cover, not an unbounded daily log."""
    validate_official_calendar(previous)
    validate_official_calendar(current)
    def parts(value):
        if value["sourceId"] == OFFICIAL_MARGIN_DATES_SOURCE:
            return [value]
        if value["sourceId"] == COMPOSITE_MARGIN_DATES_SOURCE:
            return value["evidence"]["calendars"]
        raise ValueError("rolling_calendar_bootstrap_proof_required")
    previous_parts, current_parts = parts(previous), parts(current)
    for left in previous_parts:
        for right in current_parts:
            if left != right and max(left["evidence"]["rangeStart"], right["evidence"]["rangeStart"]) <= min(
                    left["evidence"]["rangeEnd"], right["evidence"]["rangeEnd"]):
                # Reuse the official overlap validator even for a proof that
                # the later minimal-cover selection might otherwise discard.
                compose_official_margin_calendars([left, right])
    candidates = list({sha256(canonical_json_bytes(p)).hexdigest(): p
        for p in [*previous_parts, *current_parts]}.values())
    dates = sorted(set(previous["dates"] + current["dates"]))
    if target not in dates or dates.index(target) < 21:
        raise ValueError("rolling_calendar_baseline_missing")
    begin = dates[dates.index(target) - 21]
    selected, covered = [], begin
    while covered < target:
        possible = [p for p in candidates if p["evidence"]["rangeStart"] <= covered
            and p["evidence"]["rangeEnd"] > covered]
        if not possible:
            raise ValueError("rolling_calendar_gap")
        part = max(possible, key=lambda p: (p["evidence"]["rangeEnd"], sha256(canonical_json_bytes(p)).hexdigest()))
        selected.append(part)
        covered = part["evidence"]["rangeEnd"]
    if len(selected) == 1:
        return selected[0]
    return compose_official_margin_calendars(selected)


def prepare(snapshot, manifest, history_bundle, *, exchange, headers, bootstrap=False,
            source_exchange=None, source_proxy_url=""):
    active = read_active_rolling_checkpoint(exchange=exchange, headers=headers)
    if active is None:
        if not bootstrap:
            return {"status": "BOOTSTRAP_REQUIRED"}, None
        if source_exchange is None:
            raise ValueError("rolling_bootstrap_calendar_transport_missing")
        return _prepare_initial_checkpoint(snapshot, manifest, history_bundle, exchange=exchange,
            source_exchange=source_exchange, headers=headers, source_proxy_url=source_proxy_url)
    # Bootstrap permission is a cold-start fallback, not a separate mode that
    # should fail every later ordinary run after the first checkpoint exists.
    active_manifest, reference, previous_wire = active
    checkpoint, prior_bytes, _ = unpack_rolling_checkpoint(previous_wire, reference["sha256"])
    if not isinstance(checkpoint.get("officialCalendar"), dict):
        raise ValueError("rolling_checkpoint_calendar_missing")
    if checkpoint["officialCalendar"].get("sourceId") not in {
            OFFICIAL_MARGIN_DATES_SOURCE, COMPOSITE_MARGIN_DATES_SOURCE}:
        raise ValueError("rolling_checkpoint_calendar_proof_unsupported")
    current_calendar = official_calendar_from_bundle(manifest=manifest, history_bundle=history_bundle,
        target_date=snapshot["asOf"])
    calendar = extend_calendar(checkpoint["officialCalendar"], current_calendar, snapshot["asOf"])
    refs = {(r["sourceId"], r["tradeDate"]): r for r in json.loads(prior_bytes)}
    cache = {}
    # Only the just-collected three official sessions are discovered. Older
    # revisions remain pinned; a separate reconciliation handles older changes.
    for day in current_calendar["dates"]:
        for source in ("sse", "szse"):
            identity = _collected_source_identity(source, day, manifest, history_bundle,
                allow_absent=day != snapshot["asOf"])
            if identity is None:
                # A source not observed by this run cannot replace its pinned revision.
                continue
            raw_sha, expected_sha = identity
            reply = exchange.request("GET", "https://flow.xiaoker.com/api/v1/admin/history-artifact",
                headers=headers, params={"source": source, "dataset": "etf_scale",
                    "date": day, "file": "data", "indexed": "1"},
                max_bytes=2_000_000, chunk_size=65536, max_chunks=33, deadline_seconds=20)
            values = {k.lower(): v for k, v in reply.headers.items()}
            key, digest = values.get("x-flow-normalized-key"), values.get("x-flow-normalized-sha256")
            if (reply.status != 200 or not isinstance(key, str)
                    or not re.fullmatch(rf"market-data/normalized/{source}/etf_scale/{day}/[a-f0-9]{{64}}/data.csv", key)
                    or sha256(reply.content).hexdigest() != digest):
                raise ValueError("rolling_indexed_source_identity_mismatch")
            if digest != expected_sha or key.split("/")[-2] not in {
                    raw_sha, sha256(f"{raw_sha}:{digest}".encode()).hexdigest()}:
                # A concurrent source revision must not be mixed with the
                # Collector snapshot built from the earlier immutable source.
                raise ValueError("rolling_collected_source_revision_changed")
            refs[source, day] = {"sourceId": source, "tradeDate": day, "objectKey": key, "sha256": digest}
            cache[key] = reply.content
    def read(ref):
        if ref["objectKey"] in cache:
            return cache[ref["objectKey"]]
        return read_exact_etf_archive(ref, exchange=exchange, headers=headers)
    version = checkpoint["calculationVersion"]
    plan, candidate, nodes = build_from_published_checkpoint(snapshot, calendar, list(refs.values()),
        active_manifest=active_manifest, reference=reference, read_checkpoint=lambda _: previous_wire,
        read_archive=read, calculation_version=version, allow_same_day=True)
    if candidate is None:
        return {"status": "BOUNDED_RECOVERY_REQUIRED", "planStatus": plan["status"]}, None
    selected, missing = select_rolling_refs(calendar, snapshot["asOf"], list(refs.values()))
    if missing:
        raise ValueError("rolling_source_gap")
    ref_bytes, node_bytes = canonical_json_bytes(selected), encode_daily_cache(nodes)
    wire = pack_rolling_checkpoint({"version": 1, "asOf": snapshot["asOf"],
        "calculationVersion": version, "officialCalendar": calendar,
        "refsSha256": sha256(ref_bytes).hexdigest(), "cacheSha256": sha256(node_bytes).hexdigest()},
        ref_bytes, node_bytes)
    return {"status": "READY", "asOf": snapshot["asOf"]}, (calendar, wire, version)


def verify_retained_inputs(binding, *, snapshot, manifest, history_bundle):
    """Bind independent preparation to exact retained bytes before any I/O.

    This verifies input identity only; it does not grant production authority.
    """
    if (not isinstance(binding, dict) or set(binding) != {
            "contractVersion", "asOf", "snapshotSha256", "manifestSha256", "historyBundleSha256"}
            or binding.get("contractVersion") != "flow-retained-rolling-input-v1"):
        raise ValueError("rolling_retained_binding_invalid")
    for field, value in (("snapshotSha256", snapshot), ("manifestSha256", manifest),
                         ("historyBundleSha256", history_bundle)):
        if (not isinstance(binding.get(field), str)
                or not re.fullmatch(r"[a-f0-9]{64}", binding[field])
                or sha256(value).hexdigest() != binding[field]):
            raise ValueError("rolling_retained_input_hash_mismatch")
    value = json.loads(snapshot)
    try:
        day = date.fromisoformat(binding["asOf"])
    except (TypeError, ValueError):
        raise ValueError("rolling_retained_date_invalid") from None
    if (day.isoformat() != binding["asOf"] or not isinstance(value, dict)
            or value.get("asOf") != binding["asOf"]
            or not isinstance(value.get("publication"), dict)
            or value["publication"].get("status") != "confirmed"):
        raise ValueError("rolling_retained_source_invalid")


def retained_binding_from_plan(plan):
    """Read a pinned preparation scope; plan validity is not live approval."""
    from trading_desk.recovery_execution import validate_recovery_plan
    scope = validate_recovery_plan(plan)
    allocation = (scope.budget_manifest.get("rollingPreparation")
                  if isinstance(scope.budget_manifest, dict) else None)
    if (len(scope.data_dates) != 1 or "publish" not in scope.required_stages
            or not isinstance(allocation, dict)):
        raise ValueError("rolling_retained_plan_scope_invalid")
    resources = allocation.get("resourcesUpperBound")
    fields = {"d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes",
              "r2Bytes", "r2ClassA", "r2ClassB"}
    if (not isinstance(resources, dict) or set(resources) != fields
            or any(type(value) is not int or value < 0 for value in resources.values())
            or resources["workerRequests"] < BOOTSTRAP_WORKER_REQUEST_LIMIT + 1
            or resources["workerRequests"] > scope.max_requests):
        raise ValueError("rolling_retained_plan_resources_invalid")
    binding = allocation.get("inputBindings")
    if not isinstance(binding, dict) or binding.get("asOf") != scope.data_dates[0]:
        raise ValueError("rolling_retained_plan_date_mismatch")
    return binding


def verify_retained_artifact_fingerprint(plan, *, snapshot):
    """Reject a mismatched approved artifact identity before remote reads."""
    binding = retained_binding_from_plan(plan)
    source = plan["budgetManifest"].get("publishSource")
    if not isinstance(source, dict):
        raise ValueError("rolling_retained_artifact_binding_missing")
    identity = {key: source.get(key) for key in
                ("sourceRunId", "artifactId", "artifactSha256", "artifactBytes")}
    if (not isinstance(identity["sourceRunId"], str)
            or not re.fullmatch(r"[1-9][0-9]*", identity["sourceRunId"])
            or type(identity["artifactId"]) is not int or identity["artifactId"] <= 0
            or type(identity["artifactBytes"]) is not int or identity["artifactBytes"] <= 0
            or not isinstance(identity["artifactSha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", identity["artifactSha256"])):
        raise ValueError("rolling_retained_artifact_binding_invalid")
    provenance = {"contractVersion": "flow-retained-source-evidence-v1",
                  "artifact": identity, "inputBindings": binding}
    fingerprint = canonical_sha256(provenance)
    if source.get("sourceFingerprint") != fingerprint or sha256(snapshot).hexdigest() != binding["snapshotSha256"]:
        raise ValueError("rolling_retained_source_fingerprint_mismatch")
    return binding, identity, fingerprint, provenance


def retained_source_evidence(plan, *, snapshot, refs):
    """Describe this reuse operation, never relabel it as a new collection."""
    binding, identity, fingerprint, provenance = verify_retained_artifact_fingerprint(plan, snapshot=snapshot)
    if not isinstance(refs, list) or not refs:
        raise ValueError("rolling_retained_lineage_missing")
    objects = []
    for ref in refs:
        if (not isinstance(ref, dict) or ref.get("sourceId") not in {"sse", "szse"}
                or not isinstance(ref.get("tradeDate"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref["tradeDate"])
                or not isinstance(ref.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"])
                or not isinstance(ref.get("objectKey"), str)
                or not re.fullmatch(rf'market-data/normalized/{ref["sourceId"]}/etf_scale/{ref["tradeDate"]}/[a-f0-9]{{64}}/data\.csv', ref["objectKey"])):
            raise ValueError("rolling_retained_lineage_invalid")
        objects.append({"key": ref["objectKey"], "sha256": ref["sha256"],
                        "sourceDates": [ref["tradeDate"]], "sourceFingerprint": fingerprint})
    return {"schemaVersion": 1, "provenance": provenance, "sourceFingerprint": fingerprint,
        "collectorRunId": identity["sourceRunId"], "targetDate": binding["asOf"],
        "requestCount": 0, "collectionPerformed": False, "retainedInputObjects": objects,
        "files": {"dashboard-snapshot.json": {"sha256": sha256(snapshot).hexdigest(), "bytes": len(snapshot)}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--history-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--input-bindings", type=Path,
        help="exact retained input identities; validation is not production authorization")
    parser.add_argument("--recovery-plan", type=Path,
        help="validated immutable plan containing preparation allocation and input bindings; live approval is checked by the caller")
    parser.add_argument("--bootstrap", action="store_true",
        help="explicitly build the first checkpoint from one official calendar proof and pinned archive data")
    args = parser.parse_args()
    if args.recovery_plan and (args.input_bindings or not args.bootstrap):
        parser.error("recovery preparation requires --bootstrap and no separate --input-bindings")
    retained_inputs = None
    recovery_plan = None
    if args.input_bindings or args.recovery_plan:
        retained_inputs = {
            "snapshot": read_bounded(args.snapshot, 20_000_000),
            "manifest": read_bounded(args.manifest, 2_000_000),
            "history_bundle": read_bounded(args.history_bundle, 40_000_000),
        }
        recovery_plan = json.loads(read_bounded(args.recovery_plan, 2_000_000)) if args.recovery_plan else None
        binding = (retained_binding_from_plan(recovery_plan)
                   if args.recovery_plan else json.loads(read_bounded(args.input_bindings, 4096)))
        verify_retained_inputs(binding, **retained_inputs)
        if recovery_plan is not None:
            verify_retained_artifact_fingerprint(recovery_plan, snapshot=retained_inputs["snapshot"])
    from trading_desk.bounded_http import BoundedHTTPExchange
    from trading_desk.http import HttpSettings, build_session
    creds = candidate_credentials(os.environ)
    headers = {"Authorization": "Bearer " + creds["ingest_key"],
        "CF-Access-Client-Id": creds["access_client_id"],
        "CF-Access-Client-Secret": creds["access_client_secret"]}
    inner = BoundedHTTPExchange(build_session(HttpSettings(total_retries=0)))
    class Counted:
        def __init__(self):
            self.count = self.bytes = self.archive_reads = self.indexed_archive_reads = 0
            self.unindexed_archive_reads = self.source_calendar_attempts = 0
            self.request_limit = BOOTSTRAP_WORKER_REQUEST_LIMIT if args.bootstrap else NORMAL_REQUEST_LIMIT
            self.byte_limit = BOOTSTRAP_RESPONSE_BYTE_LIMIT if args.bootstrap else NORMAL_RESPONSE_BYTE_LIMIT
            self.deadline = time.monotonic() + 480 if args.bootstrap else None
            self.meter = {"d1RowsRead": 0, "d1RowsWritten": 0, "r2ClassA": 0,
                          "r2ClassB": 0, "r2Bytes": 0}
            self.meter_complete = True

        def check_deadline(self):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ValueError("rolling_bootstrap_total_deadline")

        def record_source_attempt(self):
            self.check_deadline()
            self.source_calendar_attempts += 1

        def record_external_response(self, size):
            self.bytes += size
            if self.bytes > self.byte_limit:
                raise ValueError("rolling_read_byte_limit")

        def request(self, *pos, **kw):
            self.check_deadline()
            if self.count >= self.request_limit:
                raise ValueError("rolling_read_request_limit")
            self.count += 1
            metered = bool(args.recovery_plan)
            if metered and len(pos) > 1 and isinstance(pos[1], str) and pos[1].endswith(
                    ("/history-artifact", "/etf-state", "/source-proxy")):
                kw["params"] = {**kw.get("params", {}), "metrics": "1"}
            if len(pos) > 1 and isinstance(pos[1], str) and pos[1].endswith("/history-artifact"):
                self.archive_reads += 1
                if kw.get("params", {}).get("indexed") == "1":
                    self.indexed_archive_reads += 1
                else:
                    self.unindexed_archive_reads += 1
            try:
                reply = inner.request(*pos, **kw)
            except Exception:
                self.meter_complete = False
                raise
            if metered:
                values = {key.lower(): value for key, value in reply.headers.items()}
                fields = {"d1RowsRead": "x-flow-d1-rows-read", "d1RowsWritten": "x-flow-d1-rows-written",
                          "r2ClassA": "x-flow-r2-class-a", "r2ClassB": "x-flow-r2-class-b",
                          "r2Bytes": "x-flow-r2-write-bytes"}
                for field, header in fields.items():
                    value = values.get(header)
                    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,14}", value):
                        self.meter_complete = False
                    else:
                        self.meter[field] += int(value)
            self.bytes += len(reply.content)
            if self.bytes > self.byte_limit:
                raise ValueError("rolling_read_byte_limit")
            return reply
    exchange = Counted()
    source, snapshot = None, None
    try:
        source = retained_inputs["snapshot"] if retained_inputs is not None else read_bounded(args.snapshot, 20_000_000)
        snapshot = json.loads(source)
        if snapshot.get("publication", {}).get("status") != "confirmed":
            raise ValueError("rolling_source_not_confirmed")
        manifest_bytes = retained_inputs["manifest"] if retained_inputs is not None else read_bounded(args.manifest, 2_000_000)
        history_bytes = retained_inputs["history_bundle"] if retained_inputs is not None else read_bounded(args.history_bundle, 40_000_000)
        status, ready = prepare(snapshot, json.loads(manifest_bytes),
            json.loads(history_bytes), exchange=exchange, headers=headers,
            bootstrap=args.bootstrap, source_exchange=inner,
            source_proxy_url=os.getenv("FLOW_SOURCE_PROXY_URL", ""))
        if ready:
            calendar, wire, version = ready
            write_source_bundle(args.output, snapshot, calendar, wire, version, source_bytes=source)
            if args.recovery_plan:
                _, refs_bytes, _ = unpack_rolling_checkpoint(wire, sha256(wire).hexdigest())
                evidence = retained_source_evidence(
                    recovery_plan,
                    snapshot=source, refs=json.loads(refs_bytes))
                (args.output / "retained-source-evidence.json").write_bytes(canonical_json_bytes(evidence))
    except Exception as error:
        code = str(error)
        # Broken current-source lineage remains a global quality failure.
        # Projection/readiness failures only withhold the scoped rolling cells.
        fatal = code in {"rolling_collected_source_identity_invalid", "rolling_collected_source_identity_missing",
            "archive_recipe_calendar_identity_invalid", "archive_recipe_calendar_source_hash_mismatch"}
        status = {"status": "BLOCKED_SOURCE" if fatal else "UNAVAILABLE", "reason": code if re.fullmatch(r"[a-z0-9_]{1,100}", code)
                  else "rolling_read_or_proof_unconfirmed"}
    args.report.write_bytes(canonical_json_bytes({**status, "workerRequests": exchange.count,
        "archiveReadRequests": exchange.archive_reads,
        "indexedArchiveReadRequests": exchange.indexed_archive_reads,
        "unindexedArchiveReadRequests": exchange.unindexed_archive_reads,
        "sourceCalendarRequestAttempts": exchange.source_calendar_attempts,
        "maxSourceCalendarRequestAttempts": 2 if args.bootstrap else 0,
        "maxWorkerRequests": exchange.request_limit,
        "asOf": snapshot.get("asOf") if isinstance(snapshot, dict) else None,
        "snapshotSha256": sha256(source).hexdigest() if source is not None else None,
        "responseBytes": exchange.bytes, "maxResponseBytes": exchange.byte_limit,
        **({key: value if exchange.meter_complete else None for key, value in exchange.meter.items()}
           if args.recovery_plan else {"d1RowsRead": None, "d1RowsWritten": 0}),
        "measurementStatus": "MEASURED" if args.recovery_plan and exchange.meter_complete else "UNKNOWN",
        "accountBudgetStatus": "UNKNOWN", "note": "read-only preparation is not account quota admission"}))
    if status["status"] != "READY":
        print(f"Rolling preparation {status['status']}: {status.get('reason', 'proof_unavailable')}")
    if status["status"] == "BLOCKED_SOURCE":
        raise SystemExit("current source identity failed; publication remains blocked")


if __name__ == "__main__":
    main()
