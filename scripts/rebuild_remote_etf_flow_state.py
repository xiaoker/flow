from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from trading_desk.etf_flow_state import (
    build_etf_flow_state_from_directory,
)
from trading_desk.etf_classification import CLASSIFICATION_VERSION


BUCKET = "flow"
DATABASE = "flow"
POINTER_KEY = (
    f"market-data/derived/etf-flow-state/{CLASSIFICATION_VERSION}/latest.json"
)


def _run(command: list[str], *, cwd: Path) -> str:
    environment = os.environ.copy()
    environment["WRANGLER_LOG_PATH"] = "/private/tmp/flow-wrangler-replay.log"
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _history_rows(web_root: Path) -> list[dict[str, str]]:
    output = _run(
        [
            "npx",
            "wrangler",
            "d1",
            "execute",
            DATABASE,
            "--remote",
            "--json",
            "--command",
            (
                "SELECT as_of, r2_object_key FROM historical_snapshot "
                "WHERE quality_fail=0 ORDER BY as_of ASC"
            ),
        ],
        cwd=web_root,
    )
    payload: list[dict[str, Any]] = json.loads(output)
    rows = payload[0].get("results") or []
    if not rows:
        raise RuntimeError("No quality-passed historical snapshots were indexed")
    return [
        {
            "as_of": str(row["as_of"]),
            "r2_object_key": str(row["r2_object_key"]),
        }
        for row in rows
    ]


def _download(
    row: dict[str, str],
    *,
    destination: Path,
    web_root: Path,
) -> Path:
    path = destination / f"{row['as_of']}.json"
    if not path.exists():
        _run(
            [
                "npx",
                "wrangler",
                "r2",
                "object",
                "get",
                f"{BUCKET}/{row['r2_object_key']}",
                "--file",
                str(path),
                "--remote",
            ],
            cwd=web_root,
        )
    expected = Path(row["r2_object_key"]).stem
    actual = _sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"Historical snapshot hash mismatch for {row['as_of']}: "
            f"{actual} != {expected}"
        )
    return path


def _put(web_root: Path, key: str, path: Path) -> None:
    _run(
        [
            "npx",
            "wrangler",
            "r2",
            "object",
            "put",
            f"{BUCKET}/{key}",
            "--file",
            str(path),
            "--remote",
            "--content-type",
            "application/json",
        ],
        cwd=web_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild the compact ETF daily-flow state from hash-verified R2 "
            "historical snapshots. Official raw objects are never changed."
        )
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    workspace_root = Path(__file__).resolve().parents[1]
    web_root = workspace_root / "web"
    rows = _history_rows(web_root)
    with tempfile.TemporaryDirectory(prefix="flow-etf-history-") as temporary:
        snapshot_dir = Path(temporary) / "snapshots"
        snapshot_dir.mkdir(parents=True)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(args.workers, 24))
        ) as executor:
            futures = [
                executor.submit(
                    _download,
                    row,
                    destination=snapshot_dir,
                    web_root=web_root,
                )
                for row in rows
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        state = build_etf_flow_state_from_directory(snapshot_dir)
        output = args.output or (Path(temporary) / "etf-flow-state.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        output.write_text(body, encoding="utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        as_of = state["dates"][-1]
        object_key = (
            f"market-data/derived/etf-flow-state/{CLASSIFICATION_VERSION}/"
            f"{as_of}/{digest}.json"
        )
        pointer = {
            "version": 1,
            "classificationVersion": CLASSIFICATION_VERSION,
            "objectKey": object_key,
            "sha256": digest,
            "asOf": as_of,
            "dateCount": len(state["dates"]),
            "updatedAt": state["generatedAt"],
        }
        pointer_path = output.with_name(f"{output.stem}-pointer.json")
        pointer_path.write_text(
            json.dumps(pointer, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if args.publish:
            _put(web_root, object_key, output)
            _put(web_root, POINTER_KEY, pointer_path)
        print(
            json.dumps(
                {
                    "indexedSnapshots": len(rows),
                    "stateDates": len(state["dates"]),
                    "from": state["dates"][0],
                    "to": as_of,
                    "instruments": state["quality"]["instrumentCount"],
                    "populatedValues": state["quality"]["populatedValues"],
                    "quality": state["quality"]["status"],
                    "sha256": digest,
                    "objectKey": object_key,
                    "pointerKey": POINTER_KEY,
                    "published": args.publish,
                    "output": str(output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
