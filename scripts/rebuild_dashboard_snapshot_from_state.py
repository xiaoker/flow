"""Prepare a publishable dashboard snapshot from the offline ETF state.

The ETF terminal is materialized here, during the collector/build job. The
Worker only streams the resulting immutable snapshot and never replays the
large rolling state at request time.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_desk.etf_flow_state import materialize_etf_terminal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    state_dates = [str(value) for value in state.get("dates") or []]
    if not state_dates:
        raise ValueError("state has no dates")
    if args.as_of not in state_dates:
        raise ValueError(f"state does not contain requested --as-of {args.as_of}")
    target_index = state_dates.index(args.as_of)
    if target_index != len(state_dates) - 1:
        # A state pointer can be newer than the last complete cross-market
        # dashboard day (for example, ETF shares arrive before SZSE margin).
        # Trim the offline state to the same release date instead of mixing
        # newer ETF observations into an older dashboard.
        state = dict(state)
        state["dates"] = state_dates[: target_index + 1]
        state["instruments"] = [
            {
                **item,
                "values": list(item.get("values") or [])[: target_index + 1],
                "cashValues": list(item.get("cashValues") or [])[: target_index + 1],
            }
            for item in state.get("instruments") or []
        ]

    terminal = materialize_etf_terminal(
        state,
        base_terminal=snapshot.get("etfTerminal") or {},
    )
    snapshot["asOf"] = args.as_of
    snapshot["observedAt"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
        timespec="seconds"
    )
    snapshot["gate"] = "conditional_pass"
    publication = dict(snapshot.get("publication") or {})
    publication.update(
        {
            "status": "confirmed",
            "targetAsOf": args.as_of,
            "pendingDomains": [],
            "detail": "ETF 原始份额已补齐并由离线连续官方快照物化；Worker 不再请求时重算",
        }
    )
    snapshot["publication"] = publication
    snapshot["etfTerminal"] = terminal
    for row in snapshot.get("dataStatus") or []:
        if row.get("name") in {"上交所 ETF", "深交所 ETF"}:
            row["asOf"] = args.as_of[5:]
            row["status"] = "ready"
            row["detail"] = "官方原始份额已补齐；ETF 状态由离线连续快照物化"

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
                "publishedInstruments": len(terminal.get("instruments") or []),
                "availablePeriods": terminal.get("availablePeriods", []),
                "cashFlowAvailable": terminal.get("cashFlow", {}).get("available", False),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
