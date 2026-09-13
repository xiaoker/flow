from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from trading_desk.history_bundle import build_history_bundle_from_files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json")
    )
    parser.add_argument("--output", type=Path, default=Path("history-bundle.json"))
    parser.add_argument(
        "--run-key",
        default=None,
        help="Stable pipeline run key carried into observation receipts (or FLOW_PIPELINE_RUN_KEY).",
    )
    args = parser.parse_args()
    run_key = args.run_key or os.environ.get("FLOW_PIPELINE_RUN_KEY") or None
    bundle = build_history_bundle_from_files(
        snapshot_path=args.snapshot,
        manifest_path=args.manifest,
        workspace_root=Path.cwd(),
        run_key=run_key,
    )
    args.output.write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "bytes": args.output.stat().st_size,
                "artifacts": len(bundle["history"]["artifacts"]),
                "asOf": bundle["history"]["asOf"],
                "runKey": bundle["history"].get("runKey"),
                "officialEtfDailyChanges": (
                    {
                        "rows": len(bundle["history"]["officialEtfDailyChanges"]["rows"]),
                        "expectedPreviousDate": bundle["history"]["officialEtfDailyChanges"]["expectedPreviousDate"],
                    }
                    if bundle["history"].get("officialEtfDailyChanges")
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
