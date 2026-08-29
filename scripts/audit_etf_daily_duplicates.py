"""Read-only audit for legacy D1 ``etf_daily`` duplication.

By default this script analyzes a JSON export.  ``--remote`` executes only
SELECT statements through Wrangler and is deliberately opt-in because the
query scans a large legacy table.  It produces evidence for PR 0 and does not
delete or update anything.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


SUMMARY_SQL = """
WITH rows AS (
  SELECT release_id, trade_date, market, instrument_code
  FROM etf_daily
), keys AS (
  SELECT DISTINCT trade_date, market, instrument_code FROM rows
)
SELECT
  (SELECT COUNT(*) FROM rows) AS total_rows,
  (SELECT COUNT(*) FROM keys) AS unique_business_keys,
  (SELECT COUNT(DISTINCT release_id) FROM rows) AS release_count,
  (SELECT COUNT(*) FROM rows r LEFT JOIN data_release d ON d.id = r.release_id WHERE d.id IS NULL) AS orphan_rows,
  (SELECT COUNT(*) FROM rows r JOIN data_release d ON d.id = r.release_id WHERE d.status = 'SUPERSEDED') AS superseded_rows
""".strip()


def _unwrap_wrangler_json(payload: Any) -> dict[str, int]:
    """Normalize Wrangler's ``[{results: [...]}]`` JSON envelope."""

    if isinstance(payload, list):
        if not payload:
            return {}
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                return _unwrap_wrangler_json(item["results"])
        if len(payload) == 1 and isinstance(payload[0], dict):
            return _unwrap_wrangler_json(payload[0])
        return {}
    if isinstance(payload, dict):
        row = payload
    else:
        raise ValueError("audit input must contain a Wrangler JSON object/list")
    fields = ("total_rows", "unique_business_keys", "release_count", "orphan_rows", "superseded_rows")
    return {field: int(row.get(field, 0)) for field in fields}


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    total_rows = 0
    unique_keys: set[tuple[str, str, str]] = set()
    releases: set[str] = set()
    orphan_rows = 0
    superseded_rows = 0
    for row in rows:
        code = str(row.get("instrument_code", ""))
        key = (str(row.get("trade_date", "")), str(row.get("market", "")), code)
        total_rows += 1
        unique_keys.add(key)
        releases.add(str(row.get("release_id", "")))
        if not row.get("data_release_exists", True):
            orphan_rows += 1
        if str(row.get("release_status", "")) == "SUPERSEDED":
            superseded_rows += 1
    ratio = total_rows / len(unique_keys) if unique_keys else 0.0
    return {
        "total_rows": total_rows,
        "unique_business_keys": len(unique_keys),
        "release_count": len(releases),
        "orphan_rows": orphan_rows,
        "superseded_rows": superseded_rows,
        "materialization_multiplier": round(ratio, 4),
        "redundant_rows": max(0, total_rows - len(unique_keys)),
    }


def _remote(web_root: Path, database: str) -> dict[str, int]:
    command = [
        "npx",
        "wrangler",
        "d1",
        "execute",
        database,
        "--remote",
        "--json",
        "--command",
        SUMMARY_SQL,
    ]
    result = subprocess.run(command, cwd=web_root, capture_output=True, text=True, check=True)
    return _unwrap_wrangler_json(json.loads(result.stdout))


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit legacy etf_daily duplication without mutations")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON export: list of row objects or Wrangler result")
    source.add_argument("--remote", action="store_true", help="Run a read-only Wrangler query")
    parser.add_argument("--web-root", type=Path, default=Path("web"))
    parser.add_argument("--database", default="flow")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.remote:
        report: dict[str, Any] = _remote(args.web_root, args.database)
        total = int(report.get("total_rows", 0))
        unique = int(report.get("unique_business_keys", 0))
        report["materialization_multiplier"] = round(total / unique, 4) if unique else 0.0
        report["redundant_rows"] = max(0, total - unique)
    else:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "rows" in payload:
            payload = payload["rows"]
        if not isinstance(payload, list):
            report = _unwrap_wrangler_json(payload)
            total = report.get("total_rows", 0)
            unique = report.get("unique_business_keys", 0)
            report["materialization_multiplier"] = round(total / unique, 4) if unique else 0.0
            report["redundant_rows"] = max(0, total - unique)
        else:
            report = summarize(payload)

    report["schemaVersion"] = 1
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
