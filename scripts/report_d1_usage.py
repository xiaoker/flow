"""Aggregate read-only D1 usage/query fingerprint exports.

The input is intentionally provider-neutral so the first PR can use an
exported Cloudflare Analytics response without adding a new production API.
Each record may contain snake_case or camelCase metric names and an optional
``count`` for pre-aggregated query fingerprints.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("records", "data", "results", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload]
    raise ValueError("usage input must be a JSON object or list")


def _int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in row and row[key] is not None:
            value = int(row[key])
            if value < 0:
                raise ValueError(f"{key} must be non-negative")
            return value
    return 0


def aggregate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    total_read = 0
    total_written = 0
    by_database: dict[str, dict[str, int]] = defaultdict(lambda: {"rowsRead": 0, "rowsWritten": 0, "records": 0})
    by_fingerprint: dict[str, dict[str, int]] = defaultdict(lambda: {"rowsRead": 0, "rowsWritten": 0, "count": 0})
    for row in records:
        has_multiplier = any(
            key in row and row[key] is not None for key in ("count", "calls")
        )
        multiplier = _int(row, "count", "calls") if has_multiplier else 1
        rows_read = _int(row, "rows_read", "rowsRead") * multiplier
        rows_written = _int(row, "rows_written", "rowsWritten") * multiplier
        database = str(row.get("database") or row.get("database_name") or "unknown")
        fingerprint = str(row.get("query_fingerprint") or row.get("queryFingerprint") or row.get("query_name") or "unknown")
        total_read += rows_read
        total_written += rows_written
        by_database[database]["rowsRead"] += rows_read
        by_database[database]["rowsWritten"] += rows_written
        by_database[database]["records"] += multiplier
        by_fingerprint[fingerprint]["rowsRead"] += rows_read
        by_fingerprint[fingerprint]["rowsWritten"] += rows_written
        by_fingerprint[fingerprint]["count"] += multiplier
    top = sorted(
        (
            {"fingerprint": fingerprint, **values}
            for fingerprint, values in by_fingerprint.items()
        ),
        key=lambda row: (row["rowsRead"] + row["rowsWritten"], row["fingerprint"]),
        reverse=True,
    )
    return {
        "schemaVersion": 1,
        "totals": {"rowsRead": total_read, "rowsWritten": total_written},
        "byDatabase": {key: by_database[key] for key in sorted(by_database)},
        "topFingerprints": top[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate a read-only D1 usage export")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = aggregate(_records(json.loads(args.input.read_text(encoding="utf-8"))))
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
