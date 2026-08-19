"""Filter an immutable D1 market-history query to a validated date range."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any


def _all_dates(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        candidate = value.get("trade_date")
        if isinstance(candidate, str):
            try:
                date.fromisoformat(candidate)
            except ValueError:
                pass
            else:
                found.add(candidate)
        for child in value.values():
            found.update(_all_dates(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_all_dates(child))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Select market-history dates without interpolating inputs into SQL")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise ValueError("end must not be before start")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    selected = sorted(value for value in _all_dates(payload) if args.start <= value <= args.end)
    if not selected:
        raise RuntimeError("no market-history dates in requested range")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(f"{value}\n" for value in selected), encoding="utf-8")
    print(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "dates": len(selected)}))


if __name__ == "__main__":
    main()
