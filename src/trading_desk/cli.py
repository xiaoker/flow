from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .phase0 import DEFAULT_PCF_MAX_CODES, collect_phase0
from .pipeline_runner import PipelineRunnerError, run_pipeline_request
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
    pipeline = subparsers.add_parser(
        "pipeline",
        help="Run the candidate cross-environment pipeline executor against a bounded request fixture",
    )
    pipeline.add_argument("--request", type=Path, required=True, help="JSON execution request fixture")
    pipeline.add_argument("--state-dir", type=Path, required=True, help="Isolated checkpoint directory")
    pipeline.add_argument("--output", type=Path, required=True, help="Execution result JSON")
    pipeline.add_argument(
        "--dependency-lock",
        type=Path,
        action="append",
        required=True,
        help="Dependency lock file; repeat for a multi-file lock contract",
    )
    pipeline.add_argument("--producer-build-sha", required=True, help="Exact checked-out producer SHA")
    pipeline.add_argument("--execution-mode", choices=["fixture", "dry-run", "candidate"], default="fixture",
                          help="Explicit local behavior; dry-run validates inputs without executing stages")
    pipeline.add_argument("--snapshot", type=Path, help="Pinned archived source for candidate execution")
    pipeline.add_argument("--budget-journal", type=Path, help="Retained candidate operation journal")
    pipeline.add_argument("--approved-recovery-plan", type=Path)
    pipeline.add_argument("--resume-handoff-sha256")
    source = subparsers.add_parser("pipeline-source", help="Prepare one pinned archived candidate source; no execution authority")
    source.add_argument("--destination", type=Path, required=True)
    source.add_argument("--artifact-id", required=True)
    source.add_argument("--source-run-id", required=True)
    declared = subparsers.add_parser("pipeline-request", help="Prepare the actual declared dry-run request without network I/O")
    declared.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command in {"pipeline-source", "pipeline-request"}:
        from .pipeline_entry import prepare_workflow_inputs, prepare_declared_request
        try:
            report = (prepare_declared_request(args.destination, environment=os.environ) if args.command == "pipeline-request"
                else prepare_workflow_inputs(args.destination, artifact_id=args.artifact_id,
                    source_run_id=args.source_run_id, environment=os.environ))
        except PipelineRunnerError as exc:
            print(json.dumps({"status": "BLOCKED_QUALITY", "errorCode": exc.code}))
            raise SystemExit(2) from None
        except (OSError, ValueError):
            print(json.dumps({"status": "BLOCKED_QUALITY", "errorCode": "entry_local_input_or_state_invalid"}))
            raise SystemExit(2) from None
        print(json.dumps(report))
    elif args.command == "collect-phase0":
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
    elif args.command == "pipeline":
        from .pipeline_adapters import PipelineAdapterError
        try:
            from .pipeline_entry import (read_entry_request, dry_run_entry, verify_checkout_sha,
                verify_clean_candidate_code, run_candidate_entry)
            payload = read_entry_request(args.request)
            if args.execution_mode == "dry-run":
                report = dry_run_entry(payload, producer_build_sha=args.producer_build_sha,
                    dependency_lock_paths=args.dependency_lock, project_root=Path.cwd())
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return
            if args.execution_mode == "candidate":
                if args.snapshot is None or args.budget_journal is None:
                    raise PipelineRunnerError("entry_candidate_paths_required")
                verify_checkout_sha(Path.cwd(), args.producer_build_sha)
                verify_clean_candidate_code(Path.cwd())
                report = run_candidate_entry(payload, snapshot_path=args.snapshot,
                    producer_build_sha=args.producer_build_sha, dependency_lock_paths=args.dependency_lock,
                    project_root=Path.cwd(), state_dir=args.state_dir, output_path=args.output,
                    budget_journal=args.budget_journal, cost_policy=None, environment=os.environ,
                    approved_recovery_plan=read_entry_request(args.approved_recovery_plan) if args.approved_recovery_plan else None,
                    resume_handoff_sha256=args.resume_handoff_sha256)
            else:
                report = run_pipeline_request(
                    payload,
                    state_dir=args.state_dir,
                    output_path=args.output,
                    producer_build_sha=args.producer_build_sha,
                    dependency_lock_paths=args.dependency_lock,
                    project_root=Path.cwd(),
                )
        except (PipelineRunnerError, PipelineAdapterError) as exc:
            print(json.dumps({"status": "BLOCKED_QUALITY", "errorCode": exc.code}, ensure_ascii=False))
            raise SystemExit(2) from None
        except (OSError, ValueError):
            # Private journal/path/provider exception text is not CLI evidence.
            # Do not erase or recreate retained state to make this error pass.
            print(json.dumps({"status": "BLOCKED_QUALITY", "errorCode": "entry_local_input_or_state_invalid"}))
            raise SystemExit(2) from None
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "runKey": report["context"]["identity"]["runKey"],
                    "planHash": report["plan"]["planHash"],
                    "checkpointCount": len(report["plan"]["checkpoints"]),
                    "activeReleaseChanged": report["sideEffects"]["activeReleaseChanged"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if report["status"] != "COMPLETED":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
