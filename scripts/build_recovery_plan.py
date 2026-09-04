"""Create a deterministic, bounded recovery plan.

This command is an offline planner.  It does not claim a pipeline window,
dispatch a workflow, call an official source, or write to D1/R2.  The output
is the plan that a later, approved recovery run must present unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trading_desk.recovery_execution import assert_recovery_output_path, build_recovery_plan, plan_bytes


def _budget(value: str) -> Any:
    path = Path(value)
    if path.exists():
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("budget manifest file must contain a JSON object")
        return parsed
    # Allow a small inline manifest for deterministic offline planning.  A
    # non-JSON string remains an archived R2 key and is validated by the
    # contract (it must use the approved ops-budget/ or recovery/ prefix).
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(parsed, dict):
        raise ValueError("inline budget manifest must contain a JSON object")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--data-date", action="append", dest="data_dates", required=True)
    parser.add_argument("--stage", action="append", dest="stages", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--max-dates", type=int, required=True)
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--budget-manifest", required=True)
    parser.add_argument("--budget-manifest-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = build_recovery_plan(
        campaign_id=args.campaign_id,
        recovery_id=args.recovery_id,
        run_key=args.run_key,
        data_dates=args.data_dates,
        required_stages=args.stages,
        artifact_root=args.artifact_root,
        state_dir=args.state_dir,
        max_dates=args.max_dates,
        max_requests=args.max_requests,
        deadline=args.deadline,
        budget_manifest=_budget(args.budget_manifest),
        budget_manifest_sha256=args.budget_manifest_sha256,
    )
    assert_recovery_output_path(str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded, scope_sha256 = plan_bytes(plan)
    args.output.write_bytes(encoded)
    print(json.dumps({"planHash": plan["planHash"], "scopeSha256": scope_sha256, "dataDates": plan["dataDates"], "requiredStages": plan["requiredStages"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
