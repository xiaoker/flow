"""Build a local candidate from pinned archive files and an existing cache."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re

from trading_desk.etf_replay import MAX_SOURCE_BYTES
from trading_desk.etf_rolling_refs import (
    MAX_DAILY_CACHE_BYTES, build_rolling_candidate, decode_daily_cache, encode_daily_cache,
    MAX_ROLLING_OBJECT_BYTES, pack_rolling_checkpoint, select_rolling_refs,
    unpack_rolling_checkpoint,
)
from trading_desk.read_models import canonical_json_bytes


def read_bounded(path, limit):
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("etf_rolling_input_limit")
    return value


def write_source_bundle(bundle, snapshot, calendar, wire, calculation_version, *, source_bytes=None):
    bundle.mkdir()
    source_bytes = source_bytes if source_bytes is not None else canonical_json_bytes(snapshot)
    if json.loads(source_bytes) != snapshot:
        raise ValueError("etf_rolling_source_bytes_mismatch")
    calendar_bytes = canonical_json_bytes(calendar)
    for name, data in {"dashboard-snapshot.json": source_bytes,
                       "rolling-checkpoint.gz": wire,
                       "rolling-calendar.json": calendar_bytes}.items():
        (bundle / name).write_bytes(data)
    (bundle / "rolling-publication-input.json").write_bytes(canonical_json_bytes({
        "version": 1, "asOf": snapshot["asOf"], "calculationVersion": calculation_version,
        "snapshotSha256": sha256(source_bytes).hexdigest(),
        "calendarSha256": sha256(calendar_bytes).hexdigest(),
        "checkpointSha256": sha256(wire).hexdigest(),
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "calendar", "refs", "archive", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("previous-refs", "cache", "previous-manifest", "previous-checkpoint"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--previous-checkpoint-sha256")
    parser.add_argument("--calculation-version", required=True)
    args = parser.parse_args()
    load = lambda path: json.loads(read_bounded(path, MAX_DAILY_CACHE_BYTES))

    def read(ref):
        digest = ref["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("etf_rolling_archive_identity_invalid")
        return read_bounded(args.archive / f"{digest}.csv", MAX_SOURCE_BYTES)

    snapshot, calendar, refs = load(args.snapshot), load(args.calendar), load(args.refs)
    split = (args.cache, args.previous_refs, args.previous_manifest)
    if args.previous_checkpoint:
        if any(split) or not args.previous_checkpoint_sha256:
            parser.error("compressed checkpoint requires a pinned SHA-256 and no split inputs")
        manifest, previous_bytes, cache_bytes = unpack_rolling_checkpoint(
            read_bounded(args.previous_checkpoint, MAX_ROLLING_OBJECT_BYTES),
            args.previous_checkpoint_sha256,
        )
    else:
        if not all(split) or args.previous_checkpoint_sha256:
            parser.error("provide either a pinned compressed checkpoint or all three split inputs")
        cache_bytes = read_bounded(args.cache, MAX_DAILY_CACHE_BYTES)
        previous_bytes = read_bounded(args.previous_refs, MAX_DAILY_CACHE_BYTES)
        manifest = load(args.previous_manifest)
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("calculationVersion") != args.calculation_version
            or manifest.get("asOf") not in calendar["dates"]
            or manifest["asOf"] >= snapshot["asOf"]
            or manifest.get("cacheSha256") != sha256(cache_bytes).hexdigest()
            or manifest.get("refsSha256") != sha256(previous_bytes).hexdigest()):
        raise ValueError("etf_rolling_checkpoint_mismatch")
    plan, candidate, nodes = build_rolling_candidate(
        snapshot, calendar, refs, json.loads(previous_bytes),
        decode_daily_cache(cache_bytes),
        read=read, calculation_version=args.calculation_version,
    )
    if candidate is None:
        print(json.dumps({"status": plan["status"], "blocked": plan["blocked"],
                          "requiredReadCount": plan["requiredReadCount"]}))
        return 2
    cache = encode_daily_cache(nodes)
    selected, _ = select_rolling_refs(calendar, snapshot["asOf"], refs)
    refs_bytes = canonical_json_bytes(selected)
    checkpoint = {
        "version": 1, "asOf": snapshot["asOf"], "calculationVersion": args.calculation_version,
        "cacheSha256": sha256(cache).hexdigest(), "refsSha256": sha256(refs_bytes).hexdigest(),
        "officialCalendar": calendar,
    }
    wire = pack_rolling_checkpoint(checkpoint, refs_bytes, cache)
    digest = sha256(wire).hexdigest()
    args.output.mkdir()
    for name, data in {
        "dashboard-snapshot.json": canonical_json_bytes(candidate),
        "daily-cache.json": cache, "refs.json": refs_bytes,
        "plan.json": canonical_json_bytes(plan),
        "checkpoint.json": canonical_json_bytes(checkpoint),
        f"{digest}.checkpoint.gz": wire,
    }.items():
        (args.output / name).write_bytes(data)
    # The normal Publisher consumes the original confirmed source, not the
    # scoped candidate above. Transport only the bounded proof inputs needed
    # for its independent production recomputation, not the large daily cache.
    write_source_bundle(args.output / "source-bundle", snapshot, calendar, wire,
        args.calculation_version, source_bytes=read_bounded(args.snapshot, MAX_DAILY_CACHE_BYTES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
