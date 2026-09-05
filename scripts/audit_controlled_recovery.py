#!/usr/bin/env python3
"""Run the bounded, offline R0-C/R0-D recovery inventory auditor.

The command accepts a JSON evidence bundle exported by an already bounded
read-only collector.  It intentionally has no URL/provider options and never
dispatches, migrates, publishes or changes a feature flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="local JSON evidence bundle")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/resource-budget.json"),
        help="local resource budget contract",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="isolated directory for seven bounded audit outputs",
    )
    parser.add_argument(
        "--phase",
        choices=("R0-C", "R0-D"),
        default="R0-C",
        help="evidence phase label (default: R0-C)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.input.suffix.lower() != ".json" or args.config.suffix.lower() != ".json":
        parser.error("--input and --config must be local JSON files")
    try:
        bundle = json.loads(args.input.read_text(encoding="utf-8"))
        budget_config = json.loads(args.config.read_text(encoding="utf-8"))
        # Import after parsing so `--help` cannot accidentally import a module
        # with provider-side effects.  The module itself has no such imports.
        from trading_desk.recovery_audit import RecoveryAuditError, audit_bundle, write_audit_outputs

        result = audit_bundle(bundle, budget_config=budget_config, phase=args.phase)
        write_audit_outputs(result, args.output_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        error = {"status": "ERROR", "error": type(exc).__name__, "detail": str(exc)}
        print(json.dumps(error, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    plan = result["plan"]
    evidence = result["stageEvidence"]
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "phase": evidence["phase"],
                "planHash": plan["planHash"],
                "targets": len(result["targets"]),
                "outputDir": str(args.output_dir.resolve()),
                "executionBlocked": plan["executionBlocked"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
