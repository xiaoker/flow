"""Calculate a conservative recovery budget upper bound offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trading_desk.recovery_execution import assert_recovery_output_path, estimate_recovery_budget, validate_recovery_plan


def _object(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/resource-budget.json"))
    parser.add_argument("--measured", type=Path, required=True, help="Account usage; unknown fields fail closed")
    parser.add_argument("--reserved", type=Path, required=True, help="Daily/queued reservations; unknown fields fail closed")
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    scope = validate_recovery_plan(plan)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    measured = _object(args.measured)
    reserved = _object(args.reserved)
    report = estimate_recovery_budget(
        scope=scope,
        config=config,
        retry_attempts=args.retry_attempts,
        measured=measured,
        reserved=reserved,
    )
    report["planHash"] = scope.plan_hash
    report["measuredAt"] = measured.get("measuredAt") if measured else None
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    assert_recovery_output_path(str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if report["decision"] != "allow":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
