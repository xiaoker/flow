"""Select exact archive references for a bounded rolling ETF replay."""

import json
import re
import gzip
import io
from hashlib import sha256

from .etf_period_dependencies import validate_official_calendar

MAX_DAILY_CACHE_BYTES = 20_000_000
MAX_ROLLING_OBJECT_BYTES = 2_000_000
ROLLING_PUBLICATION_CONTRACT = "flow-etf-rolling-publication-v1"


def rebuild_rolling_publication(snapshot, *, calendar, checkpoint, checkpoint_sha256,
                                calculation_version):
    """Recompute production cells from a complete, pinned current-day cache.

    This is not a v4-to-v3 conversion. The input must be the ordinary confirmed
    source snapshot, never an already materialized replay candidate. No I/O or
    fallback is allowed during this final publication proof.
    """
    from .pipeline_application import canonical_sha256

    if ("etfReplayInput" in snapshot or
            "windowEvidence" in snapshot.get("etfTerminal", {}) or
            "scopedReplay" in snapshot.get("etfTerminal", {})):
        raise ValueError("etf_rolling_production_requires_source_snapshot")
    manifest, refs_bytes, cache_bytes = unpack_rolling_checkpoint(checkpoint, checkpoint_sha256)
    if (manifest.get("asOf") != snapshot.get("asOf") or
            manifest.get("calculationVersion") != calculation_version):
        raise ValueError("etf_rolling_checkpoint_mismatch")
    refs = json.loads(refs_bytes)

    def no_read(_ref):
        raise ValueError("etf_rolling_publication_cache_incomplete")

    plan, result, _ = build_rolling_candidate(snapshot, calendar, refs, refs,
        decode_daily_cache(cache_bytes), read=no_read, calculation_version=calculation_version)
    if result is None or plan["requiredReadCount"] != 0:
        raise ValueError("etf_rolling_publication_cache_incomplete")
    evidence = result["etfTerminal"]["windowEvidence"]
    contract = {"contractVersion": ROLLING_PUBLICATION_CONTRACT, "status": "READY",
        "asOf": snapshot["asOf"], "checkpointSha256": checkpoint_sha256,
        "calendarSha256": calendar["sha256"],
        "evidenceSha256": canonical_sha256(evidence),
        "scope": {"periods": ["2w", "1m"], "metrics": ["shares"]}}
    return result, contract


def mark_rolling_unavailable(snapshot, reason):
    """Publish proven daily facts without inventing unproven rolling cells."""
    from copy import deepcopy
    if (reason not in {"rolling_bootstrap_required", "rolling_proof_unavailable"}
            or snapshot.get("publication", {}).get("status") != "confirmed"
            or "etfReplayInput" in snapshot or "windowEvidence" in snapshot.get("etfTerminal", {})
            or "scopedReplay" in snapshot.get("etfTerminal", {})):
        raise ValueError("etf_rolling_unavailable_source_invalid")
    result = deepcopy(snapshot)
    terminal = result["etfTerminal"]
    for rows, count_field in ((terminal["instruments"], "periodCoverage"),
                              (terminal["groupPeriods"], "comparableCount")):
        for row in rows:
            for period in ("2w", "1m"):
                row.setdefault("periods", {})[period] = None
                row.setdefault(count_field, {})[period] = 0
    return result, {"contractVersion": ROLLING_PUBLICATION_CONTRACT, "status": "UNAVAILABLE",
        "asOf": snapshot["asOf"], "reasonCode": reason,
        "scope": {"periods": ["2w", "1m"], "metrics": ["shares"]}}


def resolve_rolling_binding(publication, active_manifest):
    """Resolve a release-scoped binding; the receipt alone grants no authority."""
    release_id = active_manifest.get("releaseId")
    day = active_manifest.get("asOf")
    digest = active_manifest.get("manifestSha256")
    direct = "status" in publication and "releaseId" in publication
    activation = publication.get("status") if direct else publication.get("readModelActivation")
    published_id = publication.get("releaseId") if direct else publication.get("readModelReleaseId")
    if (activation != "activated"
            or (direct and publication.get("slot") != "dashboard")
            or active_manifest.get("slot") != "dashboard"
            or not isinstance(release_id, str)
            or not isinstance(day, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
            or not re.fullmatch(rf"release:{day}:[0-9a-f]{{16}}", release_id)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or published_id != release_id
            or publication.get("asOf") != day):
        raise ValueError("etf_rolling_publication_binding_mismatch")
    return f"market-data/releases/{day}/{release_id}/rolling-checkpoint/{digest}.json"


def validate_published_checkpoint_ref(reference, active_manifest, calendar, target, *, allow_same_day=False):
    """Validate a reference obtained from a trusted active dashboard read."""
    dates, _ = validate_official_calendar(calendar)
    if target not in dates or dates.index(target) == 0:
        raise ValueError("etf_rolling_calendar_baseline_missing")
    previous = dates[dates.index(target) - 1]
    allowed = {previous, target} if allow_same_day is True else {previous}
    digest = reference.get("sha256")
    if (active_manifest.get("slot") != "dashboard"
            or active_manifest.get("asOf") not in allowed
            or reference.get("asOf") != active_manifest.get("asOf")
            or not active_manifest.get("releaseId")
            or reference.get("releaseId") != active_manifest["releaseId"]
            or reference.get("manifestSha256") != active_manifest.get("manifestSha256")
            or not re.fullmatch(r"[0-9a-f]{64}", reference.get("manifestSha256") or "")
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("etf_rolling_published_checkpoint_mismatch")
    return digest


def pack_rolling_checkpoint(manifest, refs, cache):
    from .read_models import canonical_json_bytes

    if len(refs) + len(cache) > MAX_DAILY_CACHE_BYTES:
        raise ValueError("etf_rolling_cache_limit")
    ref_value, cache_value = json.loads(refs), json.loads(cache)
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or sha256(canonical_json_bytes(ref_value)).hexdigest() != manifest.get("refsSha256")
            or sha256(canonical_json_bytes(cache_value)).hexdigest() != manifest.get("cacheSha256")):
        raise ValueError("etf_rolling_checkpoint_mismatch")
    data = canonical_json_bytes({"version": 1, "manifest": manifest,
                                 "refs": ref_value, "cache": cache_value})
    if len(data) > MAX_DAILY_CACHE_BYTES:
        raise ValueError("etf_rolling_cache_limit")
    wire = gzip.compress(data, mtime=0)
    if len(wire) > MAX_ROLLING_OBJECT_BYTES:
        raise ValueError("etf_rolling_object_limit")
    return wire


def unpack_rolling_checkpoint(wire, expected_sha256):
    from .read_models import canonical_json_bytes

    if len(wire) > MAX_ROLLING_OBJECT_BYTES or sha256(wire).hexdigest() != expected_sha256:
        raise ValueError("etf_rolling_object_identity_invalid")
    with gzip.GzipFile(fileobj=io.BytesIO(wire)) as stream:
        data = stream.read(MAX_DAILY_CACHE_BYTES + 1)
    if len(data) > MAX_DAILY_CACHE_BYTES:
        raise ValueError("etf_rolling_cache_limit")
    value = json.loads(data)
    manifest = value["manifest"]
    refs, cache = (canonical_json_bytes(value[k]) for k in ("refs", "cache"))
    if (value["version"] != 1 or manifest.get("version") != 1
            or sha256(refs).hexdigest() != manifest.get("refsSha256")
            or sha256(cache).hexdigest() != manifest.get("cacheSha256")):
        raise ValueError("etf_rolling_checkpoint_mismatch")
    return manifest, refs, cache


def encode_daily_cache(nodes):
    from .read_models import canonical_json_bytes

    raw = canonical_json_bytes({
        "version": 1,
        "nodes": [[source, day, node] for (source, day), node in sorted(nodes.items())],
    })
    if len(raw) > MAX_DAILY_CACHE_BYTES or len(nodes) > 42:
        raise ValueError("etf_rolling_cache_limit")
    return raw


def decode_daily_cache(raw):
    from datetime import date

    if len(raw) > MAX_DAILY_CACHE_BYTES:
        raise ValueError("etf_rolling_cache_limit")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("nodes"), list) or len(value["nodes"]) > 42):
        raise ValueError("etf_rolling_cache_invalid")
    result = {}
    for entry in value["nodes"]:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError("etf_rolling_cache_invalid")
        source, day, node = entry
        if (source not in ("sse", "szse") or not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or not isinstance(node, dict) or (source, day) in result):
            raise ValueError("etf_rolling_cache_invalid")
        result[source, day] = node
    return result


def select_rolling_refs(calendar, target, references):
    """Return the 21-change window plus baseline, or explicit missing keys."""
    dates, _ = validate_official_calendar(calendar)
    if target not in dates or dates.index(target) < 21:
        raise ValueError("etf_rolling_calendar_baseline_missing")
    end = dates.index(target)
    required = [(source, day) for day in dates[end - 21:end + 1]
                for source in ("sse", "szse")]
    wanted = set(required)
    selected = {}
    for ref in references:
        key = (ref["sourceId"], ref["tradeDate"])
        if key not in wanted:
            continue
        if key in selected and selected[key] != ref:
            raise ValueError("etf_rolling_archive_reference_conflict")
        selected[key] = ref
    return ([selected[key] for key in required if key in selected],
            [key for key in required if key not in selected])


def resolve_rolling_refs(calendar, target, references, lookup):
    """Reuse pinned references; request at most one exact inventory batch."""
    selected, missing = select_rolling_refs(calendar, target, references)
    if not missing or len(missing) > 6:
        return selected, missing
    expected = set(missing)
    discovered = []
    for ref in lookup([{"source": source, "date": day} for source, day in missing]):
        key = (ref["sourceId"], ref["tradeDate"])
        if key not in expected:
            raise ValueError("etf_rolling_inventory_scope_mismatch")
        expected.remove(key)
        discovered.append(ref)
    return select_rolling_refs(calendar, target, selected + discovered)


def plan_daily_reuse(calendar, target, current_refs, previous_refs, available_daily):
    """Identify reusable deltas by both source identities, not date alone."""
    from .pipeline_entry import MAX_ARCHIVE_REPLAY_REQUESTS

    dates, _ = validate_official_calendar(calendar)
    selected, missing = select_rolling_refs(calendar, target, current_refs)
    current = {(r["sourceId"], r["tradeDate"]): r for r in selected}
    prior, _ = select_rolling_refs(calendar, target, previous_refs)
    previous = {(r["sourceId"], r["tradeDate"]): r for r in prior}
    available_daily = set(available_daily)
    end = dates.index(target)
    rebuild, reuse, blocked, reads = [], [], [], set()
    for a, b in zip(dates[end - 21:end], dates[end - 20:end + 1]):
        for source in ("sse", "szse"):
            pair = ((source, a), (source, b))
            key = pair[1]
            if not all(k in current for k in pair):
                blocked.append(key)
            elif key in available_daily and all(current[k] == previous.get(k) for k in pair):
                reuse.append(key)
            else:
                rebuild.append(key)
                reads.update(pair)
    within_limit = len(reads) <= MAX_ARCHIVE_REPLAY_REQUESTS
    return dict(rebuild=rebuild, reuse=reuse, blocked=blocked,
                reads=[current[k] for k in sorted(reads)] if within_limit else [],
                requiredReadCount=len(reads), missing=missing,
                status="WITHIN_READ_LIMIT" if within_limit else "BOUNDED_RECOVERY_REQUIRED")


def execute_daily_reuse(calendar, target, current_refs, previous_refs, daily, *, read, build, calculation_version):
    """Apply a bounded plan using caller-provided verified readers/builders."""
    from hashlib import sha256
    from .pipeline_application import canonical_sha256

    if not isinstance(calculation_version, str) or not calculation_version.strip():
        raise ValueError("etf_rolling_calculation_version_required")
    dates, _ = validate_official_calendar(calendar)
    predecessors = dict(zip(dates[1:], dates))
    prior = {(r["sourceId"], r["tradeDate"]): r for r in previous_refs}

    def identity(source, day, refs):
        left = refs.get((source, predecessors.get(day)))
        right = refs.get((source, day))
        if not left or not right or not left.get("sha256") or not right.get("sha256"):
            return None
        return canonical_sha256([calculation_version, left, right])

    verified = {}
    for (source, day), node in daily.items():
        fingerprint = identity(source, day, prior)
        if (fingerprint and isinstance(node, dict) and "value" in node
                and node.get("inputSha256") == fingerprint
                and node.get("valueSha256") == canonical_sha256(node["value"])):
            verified[source, day] = node
    plan = plan_daily_reuse(calendar, target, current_refs, previous_refs, verified)
    if plan["status"] != "WITHIN_READ_LIMIT":
        return plan, None
    inputs = {}
    for ref in plan["reads"]:
        data = read(ref)
        if not isinstance(data, bytes) or sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("etf_rolling_archive_hash_mismatch")
        inputs[ref["sourceId"], ref["tradeDate"]] = data
    result = {key: verified[key] for key in plan["reuse"]}
    current = {(r["sourceId"], r["tradeDate"]): r for r in current_refs}
    for source, day in plan["rebuild"]:
        value = build(source, predecessors[day], day,
                      inputs[source, predecessors[day]], inputs[source, day])
        result[source, day] = {"inputSha256": identity(source, day, current),
                               "valueSha256": canonical_sha256(value), "value": value}
    return plan, result


def materialize_rolling_daily(calendar, target, nodes, *, base_terminal=None):
    """Aggregate verified market-day nodes through the existing ETF builder."""
    from .etf_flow_state import build_etf_flow_state, materialize_etf_terminal
    from .read_models import canonical_json_bytes

    dates, _ = validate_official_calendar(calendar)
    if target not in dates or dates.index(target) < 21:
        raise ValueError("etf_rolling_calendar_baseline_missing")
    end = dates.index(target)
    snapshots = []
    for day in dates[end - 20:end + 1]:
        rows = []
        for source in ("sse", "szse"):
            node = nodes.get((source, day))
            if node is not None:
                instruments = node["value"]["instruments"]
                if any(row.get("market") != source.upper() for row in instruments):
                    raise ValueError("etf_rolling_daily_market_mismatch")
                rows.extend(instruments)
        snapshot = {"asOf": day, "etfTerminal": {"instruments": rows}}
        snapshots.append((canonical_json_bytes(snapshot), snapshot))
    state = build_etf_flow_state(snapshots, canonical_etf=True)
    state["officialCalendar"] = calendar
    if base_terminal:
        identity = {(r["market"], r["code"]): r for r in base_terminal["instruments"]}
        for row in state["instruments"]:
            original = identity.get((row["market"], row["code"]), {})
            for field in ("category", "groupType", "assetClass"):
                if field in original:
                    row[field] = original[field]
    return materialize_etf_terminal(state, base_terminal=base_terminal)


def build_rolling_candidate(snapshot, calendar, current_refs, previous_refs, daily,
                            *, read, calculation_version):
    """Build a scoped snapshot without registering or activating a release."""
    from .etf_replay import REPLAY_CONTRACT, _scoped_result, build_replay_daily_terminal
    from .pipeline_application import canonical_sha256

    if snapshot.get("publication", {}).get("status") != "confirmed":
        raise ValueError("etf_replay_scoped_base_not_confirmed")
    target = snapshot["asOf"]
    refs, _ = select_rolling_refs(calendar, target, current_refs)
    for ref in refs:
        source, day = ref["sourceId"], ref["tradeDate"]
        digest, key = ref.get("sha256"), ref.get("objectKey")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(key, str)
                or not re.fullmatch(
                    rf"market-data/normalized/{source}/etf_scale/{day}/[0-9a-f]{{64}}/data\.csv",
                    key,
                )):
            raise ValueError("etf_rolling_archive_identity_invalid")
    plan, nodes = execute_daily_reuse(
        calendar, target, current_refs, previous_refs, daily,
        read=read, build=build_replay_daily_terminal, calculation_version=calculation_version,
    )
    if nodes is None or plan["blocked"]:
        return plan, None, nodes
    terminal = materialize_rolling_daily(calendar, target, nodes,
                                         base_terminal=snapshot["etfTerminal"])
    proof = terminal["windowEvidence"]
    proof["sourceInputs"] = sorted(refs, key=lambda r: (r["tradeDate"], r["sourceId"]))
    proof["inputSha256"] = canonical_sha256({
        "contractVersion": REPLAY_CONTRACT, "calendarSha256": proof["calendarSha256"],
        "datesSha256": proof["datesSha256"], "sourceInputs": proof["sourceInputs"],
    })
    scope = {"periods": ["2w", "1m"], "metrics": ["shares"]}
    candidate = _scoped_result({**snapshot, "etfReplayInput": {}},
                               {"etfTerminal": terminal}, {"scope": scope})
    return plan, candidate, nodes


def build_from_published_checkpoint(snapshot, calendar, current_refs, *,
                                    active_manifest, reference, read_checkpoint,
                                    read_archive, calculation_version, allow_same_day=False):
    """Resume from a checkpoint reference resolved by the active-release reader."""
    digest = validate_published_checkpoint_ref(
        reference, active_manifest, calendar, snapshot["asOf"], allow_same_day=allow_same_day,
    )
    manifest, previous_refs, cache = unpack_rolling_checkpoint(
        read_checkpoint(reference), digest,
    )
    if (manifest.get("asOf") != reference["asOf"]
            or manifest.get("calculationVersion") != calculation_version):
        raise ValueError("etf_rolling_checkpoint_mismatch")
    return build_rolling_candidate(
        snapshot, calendar, current_refs, json.loads(previous_refs),
        decode_daily_cache(cache), read=read_archive,
        calculation_version=calculation_version,
    )
