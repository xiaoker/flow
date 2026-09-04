"""Validate one immutable recovery receipt without remote side effects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.recovery_execution import assert_recovery_output_path, receipt_bytes, validate_recovery_plan, validate_recovery_receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    scope = None
    if args.plan:
        scope = validate_recovery_plan(json.loads(args.plan.read_text(encoding="utf-8")))
    validated = validate_recovery_receipt(receipt, scope=scope, expected_attempt=args.attempt)
    encoded, digest = receipt_bytes(validated)
    result = {"status": "valid", "sha256": digest, "bytes": len(encoded), "receipt": validated}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        assert_recovery_output_path(str(args.output))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
