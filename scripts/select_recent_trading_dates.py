from __future__ import annotations

import argparse
from datetime import date

from trading_desk.dashboard import discover_recent_trading_dates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    print(" ".join(discover_recent_trading_dates(end_date=args.end_date, count=args.count)))


if __name__ == "__main__":
    main()
