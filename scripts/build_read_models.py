from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.read_models import write_market_read_models, write_read_models
from trading_desk.read_model_capabilities import CAPABILITY_CONTRACT_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description="Build immutable dashboard read models offline")
    parser.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("read-models"))
    parser.add_argument("--object-prefix")
    parser.add_argument("--require-materialized", action="store_true")
    parser.add_argument(
        "--slot",
        choices=("dashboard", "full"),
        default="full",
        help="Build the core dashboard slot without PCF, or the complete legacy/full model set",
    )
    parser.add_argument(
        "--stock-catalog",
        type=Path,
        help="Complete ETF-to-stock catalog sidecar generated offline (required for --slot full)",
    )
    parser.add_argument(
        "--market-history",
        type=Path,
        help="Compact market_history_daily JSON bounded to the snapshot asOf",
    )
    parser.add_argument(
        "--previous-manifest",
        type=Path,
        help="Optional active-release manifest used for history-length regression gates",
    )
    parser.add_argument(
        "--producer-build-sha",
        help="Git commit SHA of the data producer; recorded in the release manifest",
    )
    parser.add_argument(
        "--capability-contract-v07",
        action="store_true",
        help="Emit the schema-v4 v0.7 capability contract for an explicitly staged candidate",
    )
    args = parser.parse_args()
    if args.slot == "full" and args.stock_catalog is None:
        parser.error("--stock-catalog is required when --slot full is selected")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    if args.slot == "dashboard":
        result = write_market_read_models(
            snapshot,
            args.output_dir,
            require_materialized=args.require_materialized,
            object_prefix=args.object_prefix,
            market_history_path=args.market_history,
            previous_manifest_path=args.previous_manifest,
            producer_build_sha=args.producer_build_sha,
            capability_contract_version=CAPABILITY_CONTRACT_VERSION if args.capability_contract_v07 else None,
        )
    else:
        result = write_read_models(
            snapshot,
            args.output_dir,
            require_materialized=args.require_materialized,
            object_prefix=args.object_prefix,
            stock_catalog_path=args.stock_catalog,
            market_history_path=args.market_history,
            previous_manifest_path=args.previous_manifest,
            producer_build_sha=args.producer_build_sha,
            capability_contract_version=CAPABILITY_CONTRACT_VERSION if args.capability_contract_v07 else None,
        )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
