from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.dashboard import build_dashboard_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json"))
    parser.add_argument("--quality", type=Path, default=Path("reports/phase0/latest-quality-summary.json"))
    parser.add_argument("--output", type=Path, default=Path("dashboard-snapshot.json"))
    args = parser.parse_args()
    snapshot = build_dashboard_snapshot(
        manifest_path=args.manifest,
        quality_path=args.quality,
        workspace_root=Path.cwd(),
    )
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
