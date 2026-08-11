from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.dashboard import build_dashboard_snapshot
from trading_desk.etf_flow_state import materialize_dashboard_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json"))
    parser.add_argument("--quality", type=Path, default=Path("reports/phase0/latest-quality-summary.json"))
    parser.add_argument(
        "--state",
        type=Path,
        help="Optional verified rolling ETF state to materialize after the base snapshot",
    )
    parser.add_argument(
        "--stock-catalog-output",
        type=Path,
        help="Optional complete ETF-to-stock catalog sidecar for sharded read models",
    )
    parser.add_argument("--output", type=Path, default=Path("dashboard-snapshot.json"))
    args = parser.parse_args()
    snapshot = build_dashboard_snapshot(
        manifest_path=args.manifest,
        quality_path=args.quality,
        workspace_root=Path.cwd(),
        stock_catalog_path=args.stock_catalog_output,
    )
    if args.state:
        state = json.loads(args.state.read_text(encoding="utf-8"))
        snapshot = materialize_dashboard_snapshot(snapshot, state)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
