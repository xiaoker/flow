"""Validate an immutable recovery plan and print its date-major matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.recovery_execution import assert_recovery_output_path, iter_execution_matrix, validate_recovery_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-hash")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = json.loads(args.plan.read_text(encoding="utf-8"))
    scope = validate_recovery_plan(value, expected_plan_hash=args.expected_plan_hash)
    result = {
        "schemaVersion": 1,
        "status": "valid",
        "planHash": scope.plan_hash,
        "scope": scope.as_dict(),
        "executionMatrix": list(iter_execution_matrix(scope)),
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        assert_recovery_output_path(str(args.output))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
