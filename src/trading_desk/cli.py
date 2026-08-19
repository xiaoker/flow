from __future__ import annotations

import argparse
import json
from pathlib import Path

from .phase0 import DEFAULT_PCF_MAX_CODES, collect_phase0
from .release_qa import failed_findings, run_release_qa


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-desk")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser(
        "collect-phase0", help="Collect and validate exactly three trading dates"
    )
    collect.add_argument("--dates", nargs=3, required=True, metavar="YYYY-MM-DD")
    collect.add_argument(
        "--window",
        choices=["auto", "evening", "morning", "reconcile"],
        default="auto",
        help="Source release window used for audit/reconciliation metadata",
    )
    collect.add_argument(
        "--pcf-max-codes",
        type=int,
        default=DEFAULT_PCF_MAX_CODES,
        help=(
            "Maximum changed SSE ETF codes to enrich with PCF in this bounded "
            "daily window; deferred codes remain explicitly unavailable"
        ),
    )
    collect.add_argument("--data-root", default="data")
    collect.add_argument("--report-root", default="reports/phase0")
    qa = subparsers.add_parser(
        "qa-release", help="Reconcile source evidence, normalized data and dashboard snapshot"
    )
    qa.add_argument("--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json"))
    qa.add_argument("--quality", type=Path, default=Path("reports/phase0/latest-quality-summary.json"))
    qa.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    qa.add_argument("--state", type=Path, help="Verified rolling ETF state used to reproduce materialized terminal")
    qa.add_argument("--output", type=Path, default=Path("reports/phase0/latest-release-qa.json"))
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
            collection_window=args.window,
            pcf_max_codes=args.pcf_max_codes,
        )
        summary = {
            "run_id": result["manifest"]["run_id"],
            "requested_dates": result["manifest"]["requested_dates"],
            "collection_window": result["manifest"].get("collection_window", "auto"),
            "counts": result["quality"]["counts"],
            "phase0_gate_status": result["quality"]["phase0_gate_status"],
            "phase0_gate_passed": result["quality"]["phase0_gate_passed"],
            "pending_domains": result["quality"].get("pending_domains", []),
            "errors": len(result["quality"]["errors"]),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "qa-release":
        report = run_release_qa(
            manifest_path=args.manifest,
            quality_path=args.quality,
            snapshot_path=args.snapshot,
            output_path=args.output,
            workspace_root=Path.cwd(),
            state_path=args.state,
        )
        summary = {
            "run_id": report.get("run_id"),
            "verdict": report["verdict"],
            "counts": report["counts"],
            "report": str(args.output),
            "failed_checks": [
                {"check_id": item["check_id"], "dataset": item["dataset"], "message": item["message"]}
                for item in failed_findings(report)
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if report["counts"]["fail"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
