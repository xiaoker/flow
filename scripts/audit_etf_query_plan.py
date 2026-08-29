"""Generate the read-only PR 0 baseline for the legacy ETF history query.

The command uses an in-memory SQLite fixture.  It does not connect to D1 and
does not execute the expensive query; it only asks SQLite for ``EXPLAIN QUERY
PLAN`` output.  A small materialized sample keeps the normal invocation cheap
while the report records the production dimensions required by the contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.etf_query_plan import (
    EtfQueryPlanFixture,
    PRODUCTION_SCALE,
    build_query_plan_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit the legacy ETF history query plan")
    parser.add_argument("--days", type=int, default=12, help="materialized trading days")
    parser.add_argument("--etfs", type=int, default=32, help="materialized ETF count")
    parser.add_argument("--revisions", type=int, default=3, help="revisions per business key")
    parser.add_argument(
        "--production-scale",
        action="store_true",
        help="materialize the full 365 x 1600 x 5 fixture (slow; opt in explicitly)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    fixture = PRODUCTION_SCALE if args.production_scale else EtfQueryPlanFixture(
        trading_days=args.days,
        etf_count=args.etfs,
        revisions_per_business_key=args.revisions,
    )
    report = build_query_plan_report(fixture)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
