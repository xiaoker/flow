"""Prepare a publishable dashboard snapshot from the offline ETF state.

The ETF terminal is materialized here, during the collector/build job. The
Worker only streams the resulting immutable snapshot and never replays the
large rolling state at request time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.etf_flow_state import materialize_dashboard_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--official-calendar", type=Path,
        help="Explicit verified official calendar for candidate-only integrity materialization; preserves base publication status")
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if args.official_calendar:
        state["officialCalendar"] = json.loads(args.official_calendar.read_text(encoding="utf-8"))
    state_dates = [str(value) for value in state.get("dates") or []]
    snapshot = materialize_dashboard_snapshot(snapshot, state, as_of=args.as_of)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "asOf": snapshot["asOf"],
                "stateDates": len(state_dates),
                "stateInstruments": len(state.get("instruments") or []),
                "publishedInstruments": len(snapshot["etfTerminal"].get("instruments") or []),
                "availablePeriods": snapshot["etfTerminal"].get("availablePeriods", []),
                "cashFlowAvailable": snapshot["etfTerminal"].get("cashFlow", {}).get("available", False),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
