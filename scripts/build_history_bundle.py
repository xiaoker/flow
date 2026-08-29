from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.history_bundle import build_history_bundle_from_files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json")
    )
    parser.add_argument("--output", type=Path, default=Path("history-bundle.json"))
    args = parser.parse_args()
    bundle = build_history_bundle_from_files(
        snapshot_path=args.snapshot,
        manifest_path=args.manifest,
        workspace_root=Path.cwd(),
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
