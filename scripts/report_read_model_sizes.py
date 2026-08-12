"""Emit deterministic raw/gzip read-model size and payload-shape evidence.

This is deliberately an offline check.  The Worker never parses or trims a
large JSON object at request time; the report gives the producer a hard 5 MB
guard and a visible warning when a view approaches the free-plan budget.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any


HARD_LIMIT = 5_000_000
SOFT_TARGETS = {
    "bootstrap": 1_000_000,
    "overview": 250_000,
    "radar": 2_000_000,
    "radar_flow": 3_000_000,
    "pressure": 3_000_000,
    "national": 500_000,
    "stock_search": 1_000_000,
}


def _bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _field_sizes(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    return {str(key): _bytes(value) for key, value in sorted(payload.items())}


def build_report(read_model_dir: Path) -> dict[str, Any]:
    manifest = json.loads((read_model_dir / "manifest.json").read_text(encoding="utf-8"))
    artifacts: list[dict[str, Any]] = []
    hard_failures: list[str] = []
    soft_warnings: list[str] = []
    for name in manifest.get("requiredArtifacts", list((manifest.get("artifacts") or {}).keys())):
        path = read_model_dir / f"{name}.json"
        content = path.read_bytes()
        raw_bytes = len(content)
        gzip_bytes = len(gzip.compress(content, compresslevel=6))
        soft_target = SOFT_TARGETS.get(name, 2_000_000)
        entry = {
            "name": name,
            "rawBytes": raw_bytes,
            "gzipBytes": gzip_bytes,
            "hardLimitBytes": HARD_LIMIT,
            "softTargetBytes": soft_target,
            "hardLimitRatio": round(raw_bytes / HARD_LIMIT, 6),
            "softTargetRatio": round(raw_bytes / soft_target, 6),
        }
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = None
        entry["topLevelFieldBytes"] = _field_sizes(payload)
        if name in {"radar", "radar_flow"} and isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if isinstance(data, dict):
                terminal = data.get("etfTerminal")
                flow = data.get("etfStockFlow")
                if terminal is not None:
                    entry["etfTerminalBytes"] = _bytes(terminal)
                    if isinstance(terminal, dict):
                        entry["etfTerminalFieldBytes"] = _field_sizes(terminal)
                if flow is not None:
                    entry["etfStockFlowBytes"] = _bytes(flow)
        artifacts.append(entry)
        if raw_bytes > HARD_LIMIT:
            hard_failures.append(f"{name}:{raw_bytes}>{HARD_LIMIT}")
        elif raw_bytes > soft_target:
            soft_warnings.append(f"{name}:{raw_bytes}>{soft_target}")
    return {
        "schemaVersion": 1,
        "releaseId": manifest.get("releaseId"),
        "asOf": manifest.get("asOf"),
        "artifacts": artifacts,
        "softWarnings": soft_warnings,
        "hardFailures": hard_failures,
        "passed": not hard_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report immutable read-model sizes")
    parser.add_argument("--read-model-dir", type=Path, default=Path("read-models"))
    parser.add_argument("--output", type=Path, default=Path("artifact-size-report.json"))
    args = parser.parse_args()
    report = build_report(args.read_model_dir)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
