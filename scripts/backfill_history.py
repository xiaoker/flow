from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from trading_desk.backfill import run_history_backfill


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 8, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 7, 21))
    parser.add_argument("--data-root", type=Path, default=Path("data/backfill"))
    parser.add_argument(
        "--report", type=Path, default=Path("reports/backfill/latest-summary.json")
    )
    parser.add_argument(
        "--publish-url",
        default="https://flow.xiaoker.com/api/v1/admin/history-import",
    )
    parser.add_argument(
        "--dates-url",
        default="https://flow.xiaoker.com/api/v1/history/dates",
    )
    args = parser.parse_args()
    result = run_history_backfill(
        start_date=args.start,
        end_date=args.end,
        data_root=args.data_root,
        report_path=args.report,
        publish_url=args.publish_url,
        dates_url=args.dates_url,
        ingest_key=required_env("FLOW_INGEST_API_KEY"),
        access_client_id=required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        access_client_secret=required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
