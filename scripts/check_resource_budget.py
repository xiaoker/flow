"""Offline candidate admission check for the complete v0.6-B budget vector.

The command only reads JSON files and the checked-in budget contract. It does
not contact Cloudflare, GitHub or D1. A caller must provide every dimension
with an explicit basis; missing account, reservation, task or limit evidence
returns ``unknown_metrics`` and a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_desk.resource_budget import BudgetVector, ResourceLimits, evaluate_budget


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed


def build_admission_report(
    *,
    config: dict[str, Any],
    measured: dict[str, Any],
    reserved: dict[str, Any],
    task: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    accounting = config.get("resourceAccounting", {})
    ttl = accounting.get("measurementTtlSeconds")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
        raise ValueError("resourceAccounting.measurementTtlSeconds must be positive")
    limits = ResourceLimits.from_mapping(config)
    measured_vector = BudgetVector.from_mapping(
        measured.get("metrics", measured),
        default_basis="measured",
        measured_at=measured.get("measuredAt"),
        scope="account",
    )
    reserved_vector = BudgetVector.from_mapping(
        reserved.get("metrics", reserved), default_basis="estimated", scope="reservations"
    )
    task_vector = BudgetVector.from_mapping(
        task.get("metrics", task), default_basis="estimated", scope="task"
    )
    report = evaluate_budget(
        limits=limits,
        measured=measured_vector,
        reserved=reserved_vector,
        task_cost=task_vector,
        now=now or datetime.now(timezone.utc),
        measurement_ttl_seconds=ttl,
    )
    report["measurementTtlSeconds"] = ttl
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/resource-budget.json"))
    parser.add_argument("--measured", type=Path, required=True)
    parser.add_argument("--reserved", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True, help="Task upper-bound vector")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--now",
        help="Evaluation timestamp (ISO-8601); intended for deterministic offline checks",
    )
    args = parser.parse_args()
    report = build_admission_report(
        config=_read(args.config),
        measured=_read(args.measured),
        reserved=_read(args.reserved),
        task=_read(args.task),
        now=_parse_now(args.now),
    )
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if report["decision"] not in {"allow", "warn"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
