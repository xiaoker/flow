"""Build real ETF-to-stock daily partitions from archived normalized CSVs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from trading_desk.stock_flow_history import build_daily_partitions


def _read_dates(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ETF-to-stock daily history partitions")
    parser.add_argument("--shares", type=Path, required=True, help="Historical SSE/SZSE ETF share CSV")
    parser.add_argument("--pcf", type=Path, required=True, help="Historical normalized PCF CSV")
    parser.add_argument("--dates", type=Path, required=True, help="Official trading dates, one ISO date per line")
    parser.add_argument("--as-of", required=True, help="Current release date YYYY-MM-DD")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("stock-flow-history.json"))
    parser.add_argument("--max-partitions", type=int, default=21)
    parser.add_argument(
        "--lineage-report",
        type=Path,
        help="Optional fetch report used to attach per-date source keys and SHA-256 lineage",
    )
    args = parser.parse_args()

    shares = pd.read_csv(args.shares, dtype={"instrument_code": str})
    pcf = pd.read_csv(args.pcf, dtype={"etf_code": str, "stock_code": str})
    lineage_by_date = {}
    if args.lineage_report:
        lineage_payload = json.loads(args.lineage_report.read_text(encoding="utf-8"))
        if not isinstance(lineage_payload, dict) or not isinstance(lineage_payload.get("dates"), dict):
            raise ValueError("lineage report must contain a dates object")
        for trade_date, statuses in lineage_payload["dates"].items():
            if not isinstance(statuses, dict):
                continue
            for artifact_name, status in statuses.items():
                if isinstance(status, dict) and status.get("status") == "ready" and (
                    not status.get("key") or not status.get("sha256")
                ):
                    raise ValueError(f"lineage missing key or sha256:{trade_date}:{artifact_name}")
        lineage_by_date = {
            str(trade_date): {"artifacts": dict(statuses)}
            for trade_date, statuses in lineage_payload["dates"].items()
            if isinstance(statuses, dict)
        }
    index = build_daily_partitions(
        shares,
        pcf,
        _read_dates(args.dates),
        as_of=args.as_of,
        max_partitions=args.max_partitions,
        lineage_by_date=lineage_by_date,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for partition in index["partitions"]:
        date_path = args.output_dir / f"{partition['asOf']}.json"
        date_path.write_text(
            json.dumps(partition, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    # Keep the index separate from the partitions so the publisher can upload
    # each object independently and retry a single failed date.
    index["partitionFiles"] = [f"{partition['asOf']}.json" for partition in index["partitions"]]
    args.output.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "asOf": index["asOf"],
                "requestedDates": len(index["requestedDates"]),
                "availableDates": len(index["availableDates"]),
                "missingDates": len(index["missingDates"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
