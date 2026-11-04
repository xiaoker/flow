"""Read-only, bounded continuation from the active rolling checkpoint.

No collection, dispatch, claim, pointer mutation or retry. Bootstrap remains a
separate approved operation. This entry is opt-in in the existing Collector.
"""
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from trading_desk.etf_rolling_refs import (build_from_published_checkpoint,
    unpack_rolling_checkpoint, encode_daily_cache, pack_rolling_checkpoint, select_rolling_refs)
from trading_desk.etf_period_dependencies import (compose_official_margin_calendars,
    validate_official_calendar, OFFICIAL_MARGIN_DATES_SOURCE, COMPOSITE_MARGIN_DATES_SOURCE)
from trading_desk.phase0_archive_recipe import official_calendar_from_bundle
from trading_desk.pipeline_entry import read_active_rolling_checkpoint, read_exact_etf_archive, candidate_credentials
from trading_desk.read_models import canonical_json_bytes
from scripts.build_rolling_etf_candidate import write_source_bundle, read_bounded


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


def prepare(snapshot, manifest, history_bundle, *, exchange, headers):
    active = read_active_rolling_checkpoint(exchange=exchange, headers=headers)
    if active is None:
        return {"status": "BOOTSTRAP_REQUIRED"}, None
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
    def collected_identity(source, day):
        matches = [(artifact, part) for artifact in history_bundle["history"]["artifacts"]
            if artifact.get("sourceId") == source and artifact.get("dataset") == "etf_scale"
            and artifact.get("requestedDate") == day
            for part in artifact.get("normalized", [])
            if part.get("name") == "data" and part.get("tradeDates") == [day]]
        if len(matches) != 1:
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
        if (len(raw) > 2_000_000 or len(normalized) > 2_000_000
                or sha256(raw).hexdigest() != artifact["sha256"]
                or sha256(normalized).hexdigest() != part["sha256"]
                or not any(item.get("sha256") == artifact["sha256"]
                    and item.get("requested_date") == day
                    and item.get("source", {}).get("source_id") == source
                    and item.get("source", {}).get("dataset") == "etf_scale"
                    for item in manifest["artifacts"])):
            raise ValueError("rolling_collected_source_identity_invalid")
        return artifact["sha256"], part["sha256"]
    # Only the just-collected three official sessions are discovered. Older
    # revisions remain pinned; a separate reconciliation handles older changes.
    for day in current_calendar["dates"]:
        for source in ("sse", "szse"):
            raw_sha, expected_sha = collected_identity(source, day)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--history-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    from trading_desk.bounded_http import BoundedHTTPExchange
    from trading_desk.http import HttpSettings, build_session
    creds = candidate_credentials(os.environ)
    headers = {"Authorization": "Bearer " + creds["ingest_key"],
        "CF-Access-Client-Id": creds["access_client_id"],
        "CF-Access-Client-Secret": creds["access_client_secret"]}
    inner = BoundedHTTPExchange(build_session(HttpSettings(total_retries=0)))
    class Counted:
        count, bytes = 0, 0
        def request(self, *pos, **kw):
            if self.count >= 23:
                raise ValueError("rolling_read_request_limit")
            self.count += 1
            reply = inner.request(*pos, **kw)
            self.bytes += len(reply.content)
            if self.bytes > 4_000_000:
                raise ValueError("rolling_read_byte_limit")
            return reply
    exchange = Counted()
    source, snapshot = None, None
    try:
        source = read_bounded(args.snapshot, 20_000_000)
        snapshot = json.loads(source)
        if snapshot.get("publication", {}).get("status") != "confirmed":
            raise ValueError("rolling_source_not_confirmed")
        status, ready = prepare(snapshot, json.loads(read_bounded(args.manifest, 2_000_000)),
            json.loads(read_bounded(args.history_bundle, 40_000_000)), exchange=exchange, headers=headers)
        if ready:
            calendar, wire, version = ready
            write_source_bundle(args.output, snapshot, calendar, wire, version, source_bytes=source)
    except Exception as error:
        code = str(error)
        # Broken current-source lineage remains a global quality failure.
        # Projection/readiness failures only withhold the scoped rolling cells.
        fatal = code in {"rolling_collected_source_identity_invalid",
            "archive_recipe_calendar_identity_invalid", "archive_recipe_calendar_source_hash_mismatch"}
        status = {"status": "BLOCKED_SOURCE" if fatal else "UNAVAILABLE", "reason": code if re.fullmatch(r"[a-z0-9_]{1,100}", code)
                  else "rolling_read_or_proof_unconfirmed"}
    args.report.write_bytes(canonical_json_bytes({**status, "workerRequests": exchange.count,
        "asOf": snapshot.get("asOf") if isinstance(snapshot, dict) else None,
        "snapshotSha256": sha256(source).hexdigest() if source is not None else None,
        "responseBytes": exchange.bytes, "d1RowsRead": None, "d1RowsWritten": 0,
        "accountBudgetStatus": "UNKNOWN", "note": "read-only preparation is not account quota admission"}))
    if status["status"] != "READY":
        print("Rolling proof unavailable; daily publication may proceed only with explicit unavailable cells.")
    if status["status"] == "BLOCKED_SOURCE":
        raise SystemExit("current source identity failed; publication remains blocked")


if __name__ == "__main__":
    main()
