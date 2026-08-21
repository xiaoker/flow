"""Build the small source-readiness ledger for one collection run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from trading_desk.source_readiness import build_source_readiness


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a source readiness ledger")
    parser.add_argument("--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json"))
    parser.add_argument("--quality", type=Path, default=Path("reports/phase0/latest-quality-summary.json"))
    parser.add_argument("--previous", type=Path, help="Previous ledger returned by the control-plane API")
    parser.add_argument("--output", type=Path, default=Path("source-readiness.json"))
    parser.add_argument("--now", help="Override observation time (ISO instant), useful for deterministic tests")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    quality = json.loads(args.quality.read_text(encoding="utf-8"))
    previous = None
    if args.previous and args.previous.is_file():
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
    now = None
    if args.now:
        text = args.now[:-1] + "+00:00" if args.now.endswith("Z") else args.now
        now = datetime.fromisoformat(text).astimezone(timezone.utc)
    ledger = build_source_readiness(manifest, quality, previous=previous, now=now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: ledger[key] for key in ("targetDate", "state", "pendingDomains", "errorDomains", "nextCheckAt")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
