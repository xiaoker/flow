"""Create an offline, bounded ETF period dependency/rebuild plan.

Input is a reviewed JSON evidence snapshot. The command never calls Cloudflare,
GitHub, an exchange, D1, or R2; it only reads the input file and writes the
requested deterministic plan output. It does not build or publish models.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trading_desk.etf_period_dependencies import (
    MAX_PERIODS,
    build_etf_period_rebuild_plan,
)


MAX_INPUT_BYTES = 8 * 1024 * 1024


def _read_input(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("input_file_missing")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input_file_limit_exceeded")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input_json_object_required")
    required = {"calendar", "targetDates", "sourceEvidence", "existingModels"}
    if not required.issubset(value):
        raise ValueError("input_fields_missing")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic offline ETF period dependency plan from "
            "explicit official-calendar and immutable source evidence."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="reviewed, bounded evidence JSON (max 8 MiB)")
    parser.add_argument("--output", type=Path, required=True, help="local plan output JSON")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise ValueError("input_output_must_differ")
    value = _read_input(args.input)
    periods = value.get("periods", list(MAX_PERIODS))
    plan = build_etf_period_rebuild_plan(
        calendar=value["calendar"],
        target_dates=value["targetDates"],
        source_evidence=value["sourceEvidence"],
        existing_models=value["existingModels"],
        classification_version=value.get("classificationVersion"),
        formula_versions=value.get("formulaVersions"),
        periods=periods,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "planHash": plan["planHash"],
                "planStatus": plan["planStatus"],
                "executionStatus": plan["executionStatus"],
                "targetDateCount": plan["summary"]["targetDateCount"],
                "sourceGapCount": plan["summary"]["sourceGapCount"],
                "buildNodeCount": plan["summary"]["buildNodeCount"],
                "blockedNodeCount": plan["summary"]["blockedNodeCount"],
                "futureBuildCostStatus": plan["resourceEstimate"]["futureBuildCostStatus"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
