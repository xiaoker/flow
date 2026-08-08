from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from trading_desk.history_publish import publish_history_bundle


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish history artifacts as resumable chunks")
    parser.add_argument("--bundle", type=Path, default=Path("history-bundle.json"))
    parser.add_argument(
        "--url",
        default="https://flow.xiaoker.com/api/v1/admin/history-import",
    )
    args = parser.parse_args()
    bundle: dict[str, Any] = json.loads(args.bundle.read_text(encoding="utf-8"))
    result = publish_history_bundle(
        url=args.url,
        bundle=bundle,
        ingest_key=required_env("FLOW_INGEST_API_KEY"),
        access_client_id=required_env("FLOW_CF_ACCESS_CLIENT_ID"),
        access_client_secret=required_env("FLOW_CF_ACCESS_CLIENT_SECRET"),
    )
    if result.get("status") not in {"archived", "archived_pending", "waiting_sources"}:
        raise RuntimeError(f"history publication did not complete: {result}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
