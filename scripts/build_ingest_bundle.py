from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.ingest_bundle import build_ingest_bundle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("reports/phase0/latest-manifest.json")
    )
    parser.add_argument("--output", type=Path, default=Path("ingest-bundle.json"))
    parser.add_argument("--read-model-dir", type=Path)
    args = parser.parse_args()
    bundle = build_ingest_bundle(
        snapshot_path=args.snapshot,
        manifest_path=args.manifest,
        workspace_root=Path.cwd(),
        read_model_dir=args.read_model_dir,
    )
    args.output.write_text(
        json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"{args.output} ({len(bundle['archive']['artifacts'])} ETF artifacts, "
        f"{args.output.stat().st_size} bytes)"
    )


if __name__ == "__main__":
    main()
