"""Validate and canonicalize one recovery completion receipt offline.

The producer must supply evidence collected during the run.  This command
never turns a boolean ``success`` flag into a receipt and never writes a
remote object; a later publisher may upload the returned immutable bytes to
the R2 receipt key.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.recovery_execution import (
    assert_recovery_output_path,
    receipt_bytes,
    receipt_key,
    validate_recovery_plan,
    validate_recovery_receipt,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scope = validate_recovery_plan(json.loads(args.plan.read_text(encoding="utf-8")))
    value = json.loads(args.receipt.read_text(encoding="utf-8"))
    validated = validate_recovery_receipt(value, scope=scope, expected_attempt=args.attempt)
    encoded, digest = receipt_bytes(validated)
    key = receipt_key(
        recovery_id=scope.recovery_id,
        data_date=validated["dataDate"],
        stage=validated["stage"],
        attempt=validated["attemptCount"],
    )
    result = {
        "status": "valid",
        "key": key,
        "sha256": digest,
        "bytes": len(encoded),
        "receipt": validated,
    }
    assert_recovery_output_path(str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "valid", "key": key, "sha256": digest, "bytes": len(encoded)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
