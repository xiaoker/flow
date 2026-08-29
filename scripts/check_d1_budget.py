"""Report a read-only D1 budget decision for one pipeline stage.

The script consumes measured usage exported from Cloudflare Analytics (or a
small fixture) and a predicted stage cost.  It never calls Cloudflare and
never writes to D1.  It is intentionally suitable for PR 0; a later workflow
can add the account-level Analytics fetch without changing the pure decision
contract in ``trading_desk.resource_budget``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trading_desk.resource_budget import (
    ResourceLimits,
    ResourceUsage,
    decide,
    estimate_stage,
)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def build_report(
    *,
    config: dict[str, Any],
    measured: dict[str, Any] | None = None,
    predicted: dict[str, Any] | None = None,
    optional: bool = True,
) -> dict[str, Any]:
    limits = ResourceLimits.from_mapping(config)
    estimate_payload = predicted or {}
    estimate = estimate_stage(
        logical_rows=int(estimate_payload.get("logicalRows", 0)),
        index_count=int(estimate_payload.get("indexCount", 0)),
        rows_read=estimate_payload.get("rowsRead"),
        rows_written=estimate_payload.get("rowsWritten"),
        storage_bytes=int(estimate_payload.get("storageBytes", 0)),
    )
    report = decide(
        ResourceUsage.from_mapping(measured),
        estimate,
        limits,
        optional=optional,
    )
    report["schemaVersion"] = 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a read-only D1 budget decision")
    parser.add_argument("--config", type=Path, default=Path("config/resource-budget.json"))
    parser.add_argument("--measured", type=Path, help="JSON usage exported from Cloudflare Analytics")
    parser.add_argument("--predicted", type=Path, help="JSON stage estimate")
    parser.add_argument("--required", action="store_true", help="Treat the stage as required evidence")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = build_report(
        config=_read_json(args.config),
        measured=_read_json(args.measured),
        predicted=_read_json(args.predicted),
        optional=not args.required,
    )
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if report["decision"] == "block_d1_bulk":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
