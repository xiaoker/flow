"""Generate an auditable D1 index update for a repaired R2 snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-sha", required=True)
    parser.add_argument("--dashboard-key", required=True)
    parser.add_argument("--history-key", required=True)
    parser.add_argument("--manifest-key", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, default=40000)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    as_of = str(snapshot["asOf"])
    observed_at = str(snapshot["observedAt"])
    quality = snapshot.get("quality") or {}
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if args.chunk_size < 1000:
        raise ValueError("chunk-size must be at least 1000 characters")
    chunks = [payload[offset : offset + args.chunk_size] for offset in range(0, len(payload), args.chunk_size)]
    snapshot_id = as_of + ":repair-20260808"
    statements = [
        f"INSERT INTO ingest_run (id, started_at, finished_at, status, error_count, manifest_key) VALUES ({sql_string(args.run_id)}, {sql_string(observed_at)}, {sql_string(args.published_at)}, 'SUCCESS', 0, {sql_string(args.manifest_key)}) ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at, status='SUCCESS', error_count=0, manifest_key=excluded.manifest_key",
        (
            "INSERT OR REPLACE INTO dashboard_snapshot "
            "(id, as_of, observed_at, payload_json, published_at, quality_pass, quality_warn, quality_fail, r2_object_key) "
            f"VALUES ({sql_string(snapshot_id)}, {sql_string(as_of)}, {sql_string(observed_at)}, '', {sql_string(args.published_at)}, {int(quality.get('pass', 0))}, {int(quality.get('warn', 0))}, 1, {sql_string(args.dashboard_key)})"
        ),
    ]
    statements.extend(
        f"UPDATE dashboard_snapshot SET payload_json = payload_json || {sql_string(chunk)} WHERE id = {sql_string(snapshot_id)}"
        for chunk in chunks
    )
    statements.extend(
        [
            f"UPDATE dashboard_snapshot SET quality_pass={int(quality.get('pass', 0))}, quality_warn={int(quality.get('warn', 0))}, quality_fail={int(quality.get('fail', 0))} WHERE id={sql_string(snapshot_id)}",
            (
                "UPDATE historical_snapshot SET "
                f"observed_at={sql_string(observed_at)}, snapshot_sha256={sql_string(args.snapshot_sha)}, "
                f"r2_object_key={sql_string(args.history_key)}, manifest_key={sql_string(args.manifest_key)}, "
                f"source_count=2, quality_pass={int(quality.get('pass', 0))}, quality_warn={int(quality.get('warn', 0))}, quality_fail={int(quality.get('fail', 0))}, created_at={sql_string(args.published_at)} "
                f"WHERE as_of={sql_string(as_of)}"
            ),
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(";\n".join(statements) + ";\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "asOf": as_of,
        "payloadBytes": len(payload.encode("utf-8")),
        "chunks": len(chunks),
        "statements": len(statements),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
