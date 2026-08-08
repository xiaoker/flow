"""Prepare a publishable dashboard base snapshot for state hydration.

The dashboard payload is intentionally kept as a small, immutable base view:
the Worker hydrates its ETF terminal from the R2 state pointer at read time.
This utility updates the base to the repaired publication date, adds any
instruments present in the rebuilt state, and removes legacy cash estimates
that cannot be proven from the repaired raw interval.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _key(item: dict) -> str:
    return f"{item.get('market', 'SSE')}:{str(item.get('code', '')).zfill(6)}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    terminal = snapshot.get("etfTerminal") or {}
    base_instruments = list(terminal.get("instruments") or [])
    state_instruments = list(state.get("instruments") or [])
    state_by_key = {_key(item): item for item in state_instruments}
    instruments: list[dict] = []
    seen: set[str] = set()

    # Keep the existing terminal identity fields (including official latest
    # shares/NAV where present), but deliberately drop legacy derived periods.
    # The Worker will replace those fields from the R2 state.
    for original in base_instruments:
        key = _key(original)
        if key in seen:
            continue
        seen.add(key)
        item = dict(original)
        item.pop("estimatedCashPeriods", None)
        item.pop("cashPeriodCoverage", None)
        item.pop("periodCoverage", None)
        item["periods"] = {}
        instruments.append(item)

    # Repaired raw data can contain a newly listed ETF absent from the old
    # dashboard base.  Add it with identity/classification only; all derived
    # values are still calculated by the Worker from the state.
    for item in state_instruments:
        key = _key(item)
        if key in seen:
            continue
        seen.add(key)
        instruments.append(
            {
                "code": str(item["code"]).zfill(6),
                "name": str(item.get("name") or item["code"]),
                "market": item.get("market", "SSE"),
                "category": item.get("category", "其他"),
                "groupType": item.get("groupType"),
                "assetClass": item.get("assetClass"),
                "classificationSource": item.get("classificationSource"),
                "classificationConfidence": item.get("classificationConfidence"),
                "classificationRule": item.get("classificationRule"),
                "includeInGroup": item.get("includeInGroup", False),
                "shares": None,
                "nav": None,
                "navDate": None,
                "periods": {},
            }
        )

    market_counts = Counter(str(item.get("market", "SSE")) for item in state_instruments)
    category_counts = Counter(str(item.get("category", "其他")) for item in state_instruments)
    universe = dict(terminal.get("universe") or {})
    universe.update(
        {
            "total": len(state_instruments),
            "marketCounts": dict(market_counts),
            "categoryCounts": dict(category_counts),
            "periodComparable": 0,
            "cashFlowEligible": 0,
        }
    )
    terminal = dict(terminal)
    terminal.update(
        {
            "instruments": instruments,
            "dailySeries": [],
            "groupPeriods": [],
            "availablePeriods": [],
            "universe": universe,
            "cashFlow": {
                "available": False,
                "unit": "亿元",
                "method": "sum_of_daily_share_changes_times_same_day_nav",
                "eligibleInstruments": 0,
                "totalInstruments": len(state_instruments),
                "coverageDate": args.as_of,
                "warning": "本次修复未以可核验的逐日官方单位净值重建金额，金额估算暂不展示",
            },
        }
    )
    # Absence is intentional: it forces both latest and historical reads to
    # hydrate from the newly published R2 state pointer.
    terminal.pop("historyCoverage", None)

    snapshot["asOf"] = args.as_of
    snapshot["observedAt"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    snapshot["gate"] = "conditional_pass"
    publication = dict(snapshot.get("publication") or {})
    publication.update(
        {
            "status": "confirmed",
            "targetAsOf": args.as_of,
            "pendingDomains": [],
            "detail": "ETF 原始份额已补齐并从连续官方快照重建；其余市场指标沿用同一交易日已确认快照",
        }
    )
    snapshot["publication"] = publication
    snapshot["etfTerminal"] = terminal
    for row in snapshot.get("dataStatus") or []:
        if row.get("name") in {"上交所 ETF", "深交所 ETF"}:
            row["asOf"] = args.as_of[5:]
            row["status"] = "ready"
            row["detail"] = "官方原始份额已补齐；状态由连续快照重建"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "asOf": snapshot["asOf"],
        "baseInstruments": len(base_instruments),
        "stateInstruments": len(state_instruments),
        "publishedInstruments": len(instruments),
        "cashFlowAvailable": terminal["cashFlow"]["available"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
