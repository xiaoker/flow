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
    parser.add_argument("--rolling-checkpoint", type=Path,
                        help="Pinned current-day checkpoint; rebuild native production dashboard cells")
    parser.add_argument("--rolling-calendar", type=Path)
    parser.add_argument("--rolling-checkpoint-sha256")
    parser.add_argument("--rolling-calculation-version")
    parser.add_argument("--rolling-input", type=Path,
                        help="Immutable source-bundle descriptor; verifies all bounded rolling sidecars")
    parser.add_argument("--rolling-unavailable-status", type=Path,
                        help="Explicit scoped unavailability; never blocks unrelated confirmed daily facts")
    args = parser.parse_args()
    rolling_args = (args.rolling_checkpoint, args.rolling_calendar,
                    args.rolling_checkpoint_sha256, args.rolling_calculation_version)
    rolling_input = None
    unavailable_reason = None
    if args.rolling_unavailable_status:
        import hashlib
        if any(rolling_args) or args.rolling_input or args.slot != "dashboard" or args.capability_contract_v07:
            parser.error("rolling unavailable status cannot be combined with candidate/proof inputs")
        if args.rolling_unavailable_status.stat().st_size > 4096:
            parser.error("rolling unavailable status exceeds bound")
        report = json.loads(args.rolling_unavailable_status.read_bytes())
        with args.snapshot.open("rb") as stream:
            source = stream.read(20_000_001)
        if (len(source) > 20_000_000 or not isinstance(report, dict) or report.get("status") not in {
                "BOOTSTRAP_REQUIRED", "UNAVAILABLE", "BOUNDED_RECOVERY_REQUIRED"}
                or report.get("snapshotSha256") != hashlib.sha256(source).hexdigest()
                or report.get("asOf") != json.loads(source).get("asOf")):
            parser.error("rolling unavailable status does not match confirmed source")
        unavailable_reason = "rolling_bootstrap_required" if report["status"] == "BOOTSTRAP_REQUIRED" else "rolling_proof_unavailable"
    if args.rolling_input:
        import hashlib
        if any(rolling_args) or args.rolling_input.stat().st_size > 2048:
            parser.error("rolling descriptor cannot be combined with loose rolling inputs")
        descriptor = json.loads(args.rolling_input.read_bytes())
        if (not isinstance(descriptor, dict) or set(descriptor) != {
                "version", "asOf", "calculationVersion", "snapshotSha256",
                "calendarSha256", "checkpointSha256"} or descriptor["version"] != 1):
            parser.error("invalid rolling source descriptor")
        args.rolling_checkpoint = args.rolling_input.parent / "rolling-checkpoint.gz"
        args.rolling_calendar = args.rolling_input.parent / "rolling-calendar.json"
        args.rolling_checkpoint_sha256 = descriptor["checkpointSha256"]
        args.rolling_calculation_version = descriptor["calculationVersion"]
        # No file names or paths are supplied by the untrusted descriptor.
        for path, field, limit in ((args.snapshot, "snapshotSha256", 20_000_000),
                                    (args.rolling_calendar, "calendarSha256", 65536)):
            with path.open("rb") as stream:
                content = stream.read(limit + 1)
            if len(content) > limit or hashlib.sha256(content).hexdigest() != descriptor[field]:
                parser.error("rolling source descriptor hash mismatch")
        if json.loads(args.snapshot.read_bytes()).get("asOf") != descriptor["asOf"]:
            parser.error("rolling source descriptor date mismatch")
        rolling_args = (args.rolling_checkpoint, args.rolling_calendar,
                        args.rolling_checkpoint_sha256, args.rolling_calculation_version)
    if any(rolling_args):
        if not all(rolling_args) or args.slot != "dashboard" or args.capability_contract_v07:
            parser.error("rolling production requires all four rolling inputs, --slot dashboard, and no candidate flag")
        from trading_desk.etf_rolling_refs import MAX_ROLLING_OBJECT_BYTES
        with args.rolling_checkpoint.open("rb") as stream:
            checkpoint = stream.read(MAX_ROLLING_OBJECT_BYTES + 1)
        if len(checkpoint) > MAX_ROLLING_OBJECT_BYTES or args.rolling_calendar.stat().st_size > 65536:
            parser.error("rolling publication input exceeds bounded size")
        rolling_input = {"checkpoint": checkpoint,
            "checkpoint_sha256": args.rolling_checkpoint_sha256,
            "calendar": json.loads(args.rolling_calendar.read_bytes()),
            "calculation_version": args.rolling_calculation_version}
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
            rolling_publication_input=rolling_input,
            rolling_unavailable_reason=unavailable_reason,
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
