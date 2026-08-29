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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.check_d1_budget import build_report


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


def build_stage_report(
    *,
    config: dict[str, Any],
    stage: str,
    run_key: str | None = None,
    measured: dict[str, Any] | None = None,
    actions_minutes: int = 0,
    actions_run_attempt: int = 0,
    r2_bytes: int = 0,
) -> dict[str, Any]:
    if actions_minutes < 0 or actions_run_attempt < 0 or r2_bytes < 0:
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
            "actionsMinutes": actions_minutes,
            "actionsRunAttempt": actions_run_attempt,
            "r2Bytes": r2_bytes,
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
    parser.add_argument("--actions-minutes", type=int, default=0)
    parser.add_argument("--actions-run-attempt", type=int, default=0)
    parser.add_argument("--r2-bytes", type=int, default=0)
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
