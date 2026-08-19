"""Select one quality-passed historical snapshot and its predecessor.

The recovery drill deliberately selects from the D1 historical index instead
of guessing a date from the local checkout.  The output contains only the two
immutable R2 pointers needed for a bounded, read-only replay.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Any


def _rows(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("D1 response is not a result array")
    values = payload[0].get("results")
    if not isinstance(values, list):
        raise ValueError("D1 response has no result rows")
    result: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        as_of = str(value.get("as_of") or "").strip()
        key = str(value.get("r2_object_key") or "").strip()
        digest = str(value.get("snapshot_sha256") or "").strip().lower()
        if as_of and key and len(digest) == 64:
            result.append(
                {
                    "as_of": as_of,
                    "r2_object_key": key,
                    "snapshot_sha256": digest,
                }
            )
    result.sort(key=lambda row: row["as_of"])
    if len(result) < 2:
        raise ValueError("at least two quality-passed historical snapshots are required")
    return result


def select(payload: Any, *, target_date: str | None = None) -> dict[str, Any]:
    rows = _rows(payload)
    if target_date:
        matches = [row for row in rows if row["as_of"] == target_date]
        if not matches:
            raise ValueError(f"target date is not indexed as quality-passed: {target_date}")
        target = matches[0]
        selection = "explicit"
    else:
        # Never choose the first row: a predecessor is required for the
        # daily ETF-state replay and market-history comparison.
        target = secrets.choice(rows[1:])
        selection = "random"
    previous_rows = [row for row in rows if row["as_of"] < target["as_of"]]
    if not previous_rows:
        raise ValueError(f"target has no quality-passed predecessor: {target['as_of']}")
    previous = previous_rows[-1]
    return {
        "schemaVersion": 1,
        "selection": selection,
        "target": target,
        "previous": previous,
        "candidateCount": len(rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Select a bounded R2 recovery pair")
    parser.add_argument("--input", type=Path, required=True, help="wrangler D1 --json output")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-date")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = select(payload, target_date=args.target_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
