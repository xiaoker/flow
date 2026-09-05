from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_desk.stock_flow_gate import (
    StockFlowGateError,
    read_json_object,
    resolve_stock_flow_history,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the optional ETF-to-stock artifacts in a collection bundle"
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path("stock-flow-catalog.json"))
    parser.add_argument("--history", type=Path, default=Path("stock-flow-history.json"))
    parser.add_argument(
        "--history-status",
        type=Path,
        default=Path("stock-flow-history-status.json"),
    )
    args = parser.parse_args()

    def optional_json(path: Path, *, label: str) -> dict[str, object] | None:
        # Missing optional artifacts mean that penetration was not requested or
        # was explicitly unavailable.  A file that exists but is empty is a
        # broken artifact and must fail closed instead of being treated as
        # absent.
        if not path.exists():
            return None
        return read_json_object(path, label=label)

    try:
        snapshot = read_json_object(args.snapshot, label="dashboard_snapshot")
        publication = snapshot.get("publication")
        if not isinstance(publication, dict) or publication.get("status") != "confirmed":
            raise StockFlowGateError("dashboard_snapshot_not_confirmed")
        catalog = optional_json(args.catalog, label="stock_flow_catalog")
        history = optional_json(args.history, label="stock_flow_history")
        history_status = optional_json(args.history_status, label="stock_flow_history_status")
        decision = resolve_stock_flow_history(
            snapshot,
            catalog=catalog,
            history=history,
            history_status=history_status,
        )
    except StockFlowGateError as exc:
        decision = {"valid": False, "status": "BLOCKED_QUALITY", "reason": str(exc)}

    print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    if not decision.get("valid"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
