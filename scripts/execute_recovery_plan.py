"""Run the bounded recovery matrix against local checkpoint evidence.

This command is intentionally offline.  It validates the immutable plan,
loads only explicitly supplied receipt files, and emits a date-major execution
report.  It never calls D1/R2/GitHub or an official source.  The production
runner used after the R3 approval must inject the real stage runner and upload
the resulting receipts through the authenticated receipt endpoint.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_desk.recovery_execution import assert_recovery_output_path, execute_recovery_plan, validate_recovery_plan


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipt_files(root: Path | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    if not root.exists():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        value = _json(path)
        if isinstance(value, dict) and "receipt" in value and isinstance(value["receipt"], dict):
            value = value["receipt"]
        if isinstance(value, dict):
            values.append(value)
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--now", help="UTC RFC3339 instant for deterministic tests")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    scope = validate_recovery_plan(_json(args.plan))
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(timezone.utc)
    )
    result = execute_recovery_plan(
        scope,
        attempt_count=args.attempt,
        existing_receipts=_receipt_files(args.receipt_dir),
        max_requests=args.max_requests,
        now=now,
    )
    result["planHash"] = scope.plan_hash
    result["recoveryId"] = scope.recovery_id
    result["runKey"] = scope.run_key
    result["dataDates"] = list(scope.data_dates)
    result["requiredStages"] = list(scope.required_stages)
    result["offline"] = True
    result["measuredAt"] = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    assert_recovery_output_path(str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "closed": result["closed"], "requestCount": result["requestCount"], "planHash": scope.plan_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
