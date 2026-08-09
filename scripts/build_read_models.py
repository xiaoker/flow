from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.read_models import write_read_models


def main() -> None:
    parser = argparse.ArgumentParser(description="Build immutable dashboard read models offline")
    parser.add_argument("--snapshot", type=Path, default=Path("dashboard-snapshot.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("read-models"))
    parser.add_argument("--object-prefix")
    parser.add_argument("--require-materialized", action="store_true")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = write_read_models(
        snapshot,
        args.output_dir,
        require_materialized=args.require_materialized,
        object_prefix=args.object_prefix,
    )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
