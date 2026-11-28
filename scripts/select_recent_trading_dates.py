from __future__ import annotations

import argparse
from datetime import date

from trading_desk.dashboard import discover_recent_trading_dates


def select_stock_flow_dates(calendar: dict, *, target: str, count: int = 22) -> list[str]:
    """Select from verified official evidence, never cached release dates."""
    from trading_desk.etf_period_dependencies import validate_official_calendar
    verified_dates, _ = validate_official_calendar(calendar)
    dates = [day for day in verified_dates if day <= target]
    if count != 22 or len(dates) < count or dates[-1] != target:
        raise ValueError("stock_flow_official_calendar_incomplete")
    return dates[-count:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--stock-flow-calendar", type=str)
    args = parser.parse_args()
    if args.stock_flow_calendar:
        import json
        import os
        from pathlib import Path
        from scripts.prepare_rolling_etf_publication import _fetch_bootstrap_calendar, SOURCE_PROXY_URL
        from trading_desk.bounded_http import BoundedHTTPExchange
        from trading_desk.http import HttpSettings, build_session
        from trading_desk.pipeline_entry import candidate_credentials
        credentials = candidate_credentials(os.environ)
        headers = {"Authorization": "Bearer " + credentials["ingest_key"],
                   "CF-Access-Client-Id": credentials["access_client_id"],
                   "CF-Access-Client-Secret": credentials["access_client_secret"]}
        exchange = BoundedHTTPExchange(build_session(HttpSettings(total_retries=0)))
        target = args.end_date.isoformat()
        calendar = _fetch_bootstrap_calendar(target, source_exchange=exchange,
            exchange=exchange, headers=headers, source_proxy_url=SOURCE_PROXY_URL)
        dates = select_stock_flow_dates(calendar, target=target, count=args.count)
        Path(args.stock_flow_calendar).write_text(json.dumps(calendar, sort_keys=True), encoding="utf-8")
        print("\n".join(dates))
        return
    print(" ".join(discover_recent_trading_dates(end_date=args.end_date, count=args.count)))


if __name__ == "__main__":
    main()
