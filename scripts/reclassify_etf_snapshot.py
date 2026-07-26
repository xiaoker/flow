from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from trading_desk.dashboard import _build_etf_terminal_data
from trading_desk.etf_classification import (
    CLASSIFICATION_EFFECTIVE_FROM,
    CLASSIFICATION_VERSION,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild only etfTerminal from archived, quality-passed normalized "
            "ETF files while preserving every other snapshot field."
        )
    )
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--sse", type=Path, action="append", required=True)
    parser.add_argument("--szse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sql-output", type=Path)
    parser.add_argument("--published-at")
    parser.add_argument("--r2-object-key")
    args = parser.parse_args()

    snapshot = json.loads(args.base.read_text(encoding="utf-8"))
    dates = sorted(
        {
            str(value)
            for path in args.sse
            for value in pd.read_csv(path, usecols=["trade_date"])["trade_date"]
        }
    )
    if len(dates) < 2:
        raise ValueError("At least two archived SSE dates are required")

    sse_frame = pd.concat(
        [pd.read_csv(path, dtype={"instrument_code": str}) for path in args.sse],
        ignore_index=True,
    )
    szse_frame = pd.read_csv(args.szse, dtype={"instrument_code": str})
    terminal = _build_etf_terminal_data(sse_frame, szse_frame, dates)
    terminal["classificationProvenance"] = {
        "method": "derived_from_quality_passed_archived_normalized_files",
        "version": CLASSIFICATION_VERSION,
        "effectiveFrom": CLASSIFICATION_EFFECTIVE_FROM,
        "derivedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
        "baseSnapshotSha256": _sha256(args.base),
        "sourceFiles": [
            {"path": path.name, "sha256": _sha256(path)}
            for path in [*args.sse, args.szse]
        ],
    }
    snapshot["etfTerminal"] = terminal
    snapshot_body = json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":")
    )
    args.output.write_text(snapshot_body, encoding="utf-8")
    if args.sql_output:
        if not args.published_at or not args.r2_object_key:
            raise ValueError(
                "--published-at and --r2-object-key are required with --sql-output"
            )
        snapshot_digest = hashlib.sha256(snapshot_body.encode("utf-8")).hexdigest()

        def sql(value: object) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        snapshot_id = f"{snapshot['asOf']}:etf-v3:{snapshot_digest[:16]}"
        quality = snapshot["quality"]
        statements = [
            (
                "INSERT OR REPLACE INTO dashboard_snapshot ("
                "id,as_of,observed_at,payload_json,published_at,"
                "quality_pass,quality_warn,quality_fail,r2_object_key"
                ") VALUES ("
                f"{sql(snapshot_id)},{sql(snapshot['asOf'])},"
                f"{sql(snapshot['observedAt'])},'',"
                f"{sql(args.published_at)},{int(quality['pass'])},"
                f"{int(quality['warn'])},1,"
                f"{sql(args.r2_object_key)}"
                ")"
            ),
        ]
        for start in range(0, len(snapshot_body), 40_000):
            chunk = snapshot_body[start : start + 40_000]
            statements.append(
                "UPDATE dashboard_snapshot "
                f"SET payload_json=payload_json||{sql(chunk)} "
                f"WHERE id={sql(snapshot_id)}"
            )
        statements.append(
            "UPDATE dashboard_snapshot "
            f"SET quality_fail={int(quality['fail'])} "
            f"WHERE id={sql(snapshot_id)}"
        )
        statement = ";\n".join(statements) + ";\n"
        args.sql_output.write_text(statement, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
