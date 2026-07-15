from __future__ import annotations

import argparse
import json
from pathlib import Path

from .phase0 import collect_phase0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-desk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser(
        "collect-phase0", help="Collect and validate exactly three trading dates"
    )
    collect.add_argument("--dates", nargs=3, required=True, metavar="YYYY-MM-DD")
    collect.add_argument("--data-root", default="data")
    collect.add_argument("--report-root", default="reports/phase0")
    collect.add_argument(
        "--szse-etf-inferred-date",
        help="Expected date for SZSE's undated current ETF snapshot",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "collect-phase0":
        workspace_root = Path.cwd()
        result = collect_phase0(
            dates=args.dates,
            data_root=Path(args.data_root),
            report_root=Path(args.report_root),
            workspace_root=workspace_root,
            szse_etf_inferred_date=args.szse_etf_inferred_date,
        )
        summary = {
            "run_id": result["manifest"]["run_id"],
            "requested_dates": result["manifest"]["requested_dates"],
            "counts": result["quality"]["counts"],
            "phase0_gate_status": result["quality"]["phase0_gate_status"],
            "phase0_gate_passed": result["quality"]["phase0_gate_passed"],
            "errors": len(result["quality"]["errors"]),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
