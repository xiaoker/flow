"""Filter an immutable D1 market-history query to a validated date range."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

MAX_INVENTORY_DATES = 5
MAX_RANGE_DAYS = 7


def _all_dates(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        candidate = value.get("trade_date")
        if isinstance(candidate, str):
            try:
                parsed = date.fromisoformat(candidate)
            except ValueError:
                pass
            else:
                if parsed.isoformat() == candidate:
                    found.add(candidate)
        for child in value.values():
            found.update(_all_dates(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_all_dates(child))
    return found


def _strict_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("dates must use exact YYYY-MM-DD format")
    return parsed


def _validated_range(start_value: str, end_value: str) -> tuple[date, date]:
    start = _strict_date(start_value)
    end = _strict_date(end_value)
    if end < start:
        raise ValueError("end must not be before start")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range must not exceed {MAX_RANGE_DAYS} calendar days")
    return start, end


def _explicit_dates(raw: str) -> list[str]:
    values = [line.strip() for line in raw.splitlines() if line.strip()]
    if not values:
        raise ValueError("explicit dates must contain at least one date")
    for value in values:
        _strict_date(value)
    if len(set(values)) != len(values):
        raise ValueError("explicit dates must not contain duplicates")
    if len(values) > MAX_INVENTORY_DATES:
        raise ValueError(f"explicit inventory is limited to {MAX_INVENTORY_DATES} dates")
    return sorted(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Select a bounded set of market-history dates")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="D1 date query JSON for a bounded range")
    mode.add_argument("--explicit-dates", help="Newline-separated exact dates; does not infer trading days")
    mode.add_argument("--validate-range", action="store_true", help="Validate start/end before a bounded D1 query")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    if args.validate_range:
        if not args.start or not args.end or args.output or args.input or args.explicit_dates is not None:
            raise ValueError("--validate-range requires only --start and --end")
        start, end = _validated_range(args.start, args.end)
        print(json.dumps({"mode": "bounded_range", "start": start.isoformat(), "end": end.isoformat()}))
        return

    if args.output is None:
        raise ValueError("--output is required")
    if args.explicit_dates is not None:
        if args.start or args.end:
            raise ValueError("explicit dates cannot be combined with --start or --end")
        selected = _explicit_dates(args.explicit_dates)
        summary = {"mode": "explicit_dates", "dates": len(selected)}
    else:
        if args.input is None or not args.start or not args.end:
            raise ValueError("--input, --start and --end are required for range selection")
        start, end = _validated_range(args.start, args.end)
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        selected = sorted(value for value in _all_dates(payload) if args.start <= value <= args.end)
        if not selected:
            raise RuntimeError("no market-history dates in requested range")
        if len(selected) > MAX_INVENTORY_DATES:
            raise ValueError(f"inventory is limited to {MAX_INVENTORY_DATES} dates; split the range")
        summary = {"mode": "market_history_range", "start": start.isoformat(), "end": end.isoformat(), "dates": len(selected)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(f"{value}\n" for value in selected), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
