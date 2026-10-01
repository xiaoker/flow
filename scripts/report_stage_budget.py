"""Write a provider-neutral budget receipt for one pipeline stage.

The receipt is deliberately offline: it reads the checked-in budget contract
and optional measured usage, then records the D1 estimate together with R2 and
GitHub Actions counters supplied by the workflow.  It never calls Cloudflare,
GitHub or D1, so a failed report cannot hide a data write or create another
quota-consuming request.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .check_d1_budget import build_report
else:
    # GitHub Actions invokes this file directly (``python scripts/...``), so
    # the script directory rather than the repository root is sys.path[0].
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from check_d1_budget import build_report
from trading_desk.resource_budget import BudgetVector


def _read_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _stage_estimate(config: dict[str, Any], stage: str) -> dict[str, int]:
    budgets = config.get("stageBudgets")
    if not isinstance(budgets, dict) or not isinstance(budgets.get(stage), dict):
        raise ValueError(f"stage budget is not configured: {stage}")
    budget = budgets[stage]
    rows_read = int(budget.get("rowsRead", 0))
    rows_written = int(budget.get("rowsWritten", 0))
    if rows_read < 0 or rows_written < 0:
        raise ValueError(f"stage budget must be non-negative: {stage}")
    return {"rowsRead": rows_read, "rowsWritten": rows_written}


def _stage_cost_upper_bound(
    config: dict[str, Any],
    stage: str,
    *,
    actions_minutes: int | None,
    r2_bytes: int | None,
) -> BudgetVector:
    """Build a complete upper-bound vector without inventing missing costs."""

    d1 = _stage_estimate(config, stage)
    values: dict[str, Any] = {
        "d1RowsRead": {"value": d1["rowsRead"], "basis": "estimated", "scope": f"stage:{stage}"},
        "d1RowsWritten": {"value": d1["rowsWritten"], "basis": "estimated", "scope": f"stage:{stage}"},
        "workerRequests": {"value": None, "basis": "unknown", "reason": "stage_worker_requests_not_measured"},
        "actionsMinutes": {
            "value": actions_minutes if actions_minutes and actions_minutes > 0 else None,
            "basis": "measured" if actions_minutes and actions_minutes > 0 else "unknown",
            "reason": "actions_minutes_not_measured" if not actions_minutes else None,
        },
        "r2Bytes": {
            "value": r2_bytes if r2_bytes and r2_bytes > 0 else None,
            "basis": "measured" if r2_bytes and r2_bytes > 0 else "unknown",
            "reason": "r2_bytes_not_measured" if not r2_bytes else None,
        },
        "r2ClassA": {"value": None, "basis": "unknown", "reason": "r2_class_a_not_measured"},
        "r2ClassB": {"value": None, "basis": "unknown", "reason": "r2_class_b_not_measured"},
    }
    # Drop optional None metadata so BudgetMetric validation receives a clean
    # object while keeping the value/basis distinction explicit.
    for metric in values.values():
        if isinstance(metric, dict) and metric.get("reason") is None:
            metric.pop("reason", None)
    return BudgetVector.from_mapping(values, scope=f"stage:{stage}")


def build_stage_report(
    *,
    config: dict[str, Any],
    stage: str,
    run_key: str | None = None,
    measured: dict[str, Any] | None = None,
    actions_minutes: int | None = None,
    actions_run_attempt: int = 0,
    r2_bytes: int | None = None,
) -> dict[str, Any]:
    if (actions_minutes is not None and actions_minutes < 0) or actions_run_attempt < 0 or (r2_bytes is not None and r2_bytes < 0):
        raise ValueError("actions_minutes, actions_run_attempt and r2_bytes must be non-negative")
    estimate = _stage_estimate(config, stage)
    decision = build_report(
        config=config,
        measured=measured or {},
        predicted=estimate,
        # Exact PCF capture is required evidence; only parity/backfill-style
        # stages should be eligible for a hard-gate defer decision.
        optional=stage not in {"coreCollection", "publish", "pcfExact"},
    )
    decision.update(
        {
            "stage": stage,
            "runKey": run_key,
            # A workflow-provided zero is a legacy placeholder, not a
            # measured zero. Keep it unknown until a runner supplies a real
            # elapsed-minute counter.
            "actionsMinutes": actions_minutes if actions_minutes and actions_minutes > 0 else None,
            "actionsRunAttempt": actions_run_attempt,
            "r2Bytes": r2_bytes if r2_bytes and r2_bytes > 0 else None,
            "resourceMetrics": _stage_cost_upper_bound(
                config,
                stage,
                actions_minutes=actions_minutes,
                r2_bytes=r2_bytes,
            ).as_dict(),
            "recordedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    return decision


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/resource-budget.json"))
    parser.add_argument("--stage", required=True)
    parser.add_argument("--run-key")
    parser.add_argument("--measured", type=Path)
    parser.add_argument("--actions-minutes", type=int, default=None)
    parser.add_argument("--actions-run-attempt", type=int, default=0)
    parser.add_argument("--r2-bytes", type=int, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config = _read_object(args.config)
    report = build_stage_report(
        config=config,
        stage=args.stage,
        run_key=args.run_key,
        measured=_read_object(args.measured),
        actions_minutes=args.actions_minutes,
        actions_run_attempt=args.actions_run_attempt,
        r2_bytes=args.r2_bytes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if report["decision"] == "block_d1_bulk":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
