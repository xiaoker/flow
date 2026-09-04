"""Create one immutable, canonical recovery receipt from measured evidence.

The live runner and publisher construct the descriptor from files produced by
the same run.  This command validates the approved scope before serializing
the receipt, writes only below an isolated recovery directory, and emits the
R2 key/hash metadata needed by the receipt endpoint.  It performs no remote
write and never infers completion from a workflow boolean.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trading_desk.recovery_execution import (
    assert_recovery_output_path,
    receipt_bytes,
    receipt_key,
    validate_recovery_plan,
    validate_recovery_receipt,
)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True, help="Measured receipt descriptor JSON")
    parser.add_argument("--plan", type=Path, required=True, help="Approved recovery plan JSON")
    parser.add_argument("--expected-plan-hash", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Canonical raw receipt bytes")
    parser.add_argument("--metadata-output", type=Path, required=True)
    args = parser.parse_args()

    scope = validate_recovery_plan(_read(args.plan), expected_plan_hash=args.expected_plan_hash)
    receipt = validate_recovery_receipt(
        _read(args.receipt),
        scope=scope,
        expected_attempt=args.attempt,
    )
    encoded, digest = receipt_bytes(receipt)
    key = receipt_key(
        recovery_id=scope.recovery_id,
        data_date=receipt["dataDate"],
        stage=receipt["stage"],
        attempt=receipt["attemptCount"],
    )
    assert_recovery_output_path(str(args.output))
    assert_recovery_output_path(str(args.metadata_output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    metadata = {
        "schemaVersion": 1,
        "status": "valid",
        "key": key,
        "sha256": digest,
        "bytes": len(encoded),
        "dataDate": receipt["dataDate"],
        "stage": receipt["stage"],
        "attemptCount": receipt["attemptCount"],
    }
    args.metadata_output.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
