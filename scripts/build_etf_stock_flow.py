"""Build a bounded ETF-to-stock flow artifact from verified local CSVs.

This helper is intentionally offline: it never calls an exchange and never
falls back to a different PCF date.  The daily workflow can pass its combined
official ETF shares and same-day SSE PCF normalized files to the same function.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from trading_desk.etf_stock_flow import StockFlowLimits, build_stock_flow


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a date-safe ETF-to-stock flow read model")
    parser.add_argument("--shares", type=Path, required=True, help="Official ETF share CSV")
    parser.add_argument("--pcf", type=Path, required=True, help="Same-day SSE PCF normalized CSV")
    parser.add_argument("--as-of", required=True, help="Share/PCF trading date YYYY-MM-DD")
    parser.add_argument("--previous-date", help="Previous official ETF share date YYYY-MM-DD")
    parser.add_argument("--max-stocks", type=int, default=500)
    parser.add_argument("--max-contributors", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("etf-stock-flow.json"))
    args = parser.parse_args()
    result = build_stock_flow(
        pd.read_csv(args.shares, dtype={"instrument_code": str}),
        pd.read_csv(args.pcf, dtype={"etf_code": str, "stock_code": str}),
        as_of=args.as_of,
        previous_date=args.previous_date,
        limits=StockFlowLimits(
            max_stocks=args.max_stocks,
            max_contributors=args.max_contributors,
        ),
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "available": result["available"], "warnings": result["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
