from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"


def _run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=WEB_ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )


def _history_rows() -> list[dict[str, str]]:
    result = _run(
        "npx",
        "wrangler",
        "d1",
        "execute",
        "flow",
        "--remote",
        "--json",
        "--command",
        (
            "SELECT as_of, r2_object_key, snapshot_sha256 FROM historical_snapshot "
            "WHERE quality_fail = 0 ORDER BY as_of ASC;"
        ),
        capture=True,
    )
    payload = json.loads(result.stdout)
    return [
        {
            "as_of": str(row["as_of"]),
            "r2_object_key": str(row["r2_object_key"]),
            "snapshot_sha256": str(row["snapshot_sha256"]),
        }
        for row in payload[0]["results"]
    ]


def _download(row: dict[str, str], destination: Path) -> Path:
    path = destination / f"{row['as_of']}.json"
    if path.is_file() and path.stat().st_size > 0:
        return path
    _run(
        "npx",
        "wrangler",
        "r2",
        "object",
        "get",
        f"flow/{row['r2_object_key']}",
        "--remote",
        "--file",
        str(path),
    )
    return path


def _finite(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _exact_point(items: list[dict[str, Any]], as_of: str) -> dict[str, Any] | None:
    expected = {as_of, as_of[5:]}
    return next(
        (item for item in reversed(items) if str(item.get("date") or "") in expected),
        None,
    )


def _sql_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _record(path: Path, metadata: dict[str, str]) -> tuple[list[Any], bool, bool]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    as_of = str(snapshot["asOf"])
    margin = _exact_point(list(snapshot.get("marginSeries") or []), as_of)
    connect = _exact_point(list(snapshot.get("connectSeries") or []), as_of)
    values: list[Any] = [
        as_of,
        _finite((margin or {}).get("balance")),
        _finite((margin or {}).get("change")),
        _finite((margin or {}).get("financingBuy")),
        _finite((margin or {}).get("lendingBalance")),
        _finite((margin or {}).get("lendingSellVolume")),
        _finite((margin or {}).get("totalBalance")),
        _finite((margin or {}).get("sseBalance")),
        _finite((margin or {}).get("szseBalance")),
        _finite((connect or {}).get("southboundNet")),
        _finite((connect or {}).get("southboundTurnover")),
        _finite((connect or {}).get("southboundSseNet")),
        _finite((connect or {}).get("southboundSzseNet")),
        _finite((connect or {}).get("northboundTurnover")),
        _finite((connect or {}).get("northboundEtfTurnover")),
        _finite((connect or {}).get("northboundTradeCount")),
        1 if margin else 0,
        1 if connect else 0,
        metadata["snapshot_sha256"],
        str(snapshot.get("observedAt") or ""),
        str(snapshot.get("observedAt") or ""),
    ]
    return values, bool(margin), bool(connect)


def _write_sql(records: list[list[Any]], path: Path) -> None:
    columns = """
      trade_date, margin_balance, margin_change, financing_buy,
      lending_balance, lending_sell_volume, total_balance, sse_balance,
      szse_balance, southbound_net, southbound_turnover,
      southbound_sse_net, southbound_szse_net, northbound_turnover,
      northbound_etf_turnover, northbound_trade_count, margin_available,
      connect_available, source_snapshot_sha256, observed_at, created_at
    """.strip()
    clauses = [
        "INSERT INTO market_history_daily (" + columns + ") VALUES",
        ",\n".join(
            "(" + ", ".join(_sql_value(value) for value in record) + ")"
            for record in records
        ),
        """ON CONFLICT(trade_date) DO UPDATE SET
          margin_balance=excluded.margin_balance,
          margin_change=excluded.margin_change,
          financing_buy=excluded.financing_buy,
          lending_balance=excluded.lending_balance,
          lending_sell_volume=excluded.lending_sell_volume,
          total_balance=excluded.total_balance,
          sse_balance=excluded.sse_balance,
          szse_balance=excluded.szse_balance,
          southbound_net=excluded.southbound_net,
          southbound_turnover=excluded.southbound_turnover,
          southbound_sse_net=excluded.southbound_sse_net,
          southbound_szse_net=excluded.southbound_szse_net,
          northbound_turnover=excluded.northbound_turnover,
          northbound_etf_turnover=excluded.northbound_etf_turnover,
          northbound_trade_count=excluded.northbound_trade_count,
          margin_available=excluded.margin_available,
          connect_available=excluded.connect_available,
          source_snapshot_sha256=excluded.source_snapshot_sha256,
          observed_at=excluded.observed_at,
          created_at=excluded.created_at;""",
    ]
    path.write_text("\n".join(clauses), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild compact D1 margin/connect history from immutable R2 snapshots."
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows = _history_rows()
    if not rows:
        raise RuntimeError("No quality-passed historical snapshots were found")
    with tempfile.TemporaryDirectory(prefix="flow-market-history-") as directory:
        root = Path(directory)
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as pool:
            futures = {pool.submit(_download, row, root): row for row in rows}
            for index, future in enumerate(as_completed(futures), start=1):
                future.result()
                if index % 50 == 0 or index == len(futures):
                    print(f"downloaded {index}/{len(futures)}", flush=True)

        parsed = [
            _record(root / f"{row['as_of']}.json", row)
            for row in rows
        ]
        records = [item[0] for item in parsed]
        sql_path = root / "market-history.sql"
        _write_sql(records, sql_path)
        summary = {
            "snapshots": len(records),
            "first": records[0][0],
            "latest": records[-1][0],
            "marginPoints": sum(item[1] for item in parsed),
            "connectPoints": sum(item[2] for item in parsed),
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if args.apply:
            _run(
                "npx",
                "wrangler",
                "d1",
                "execute",
                "flow",
                "--remote",
                "--file",
                str(sql_path),
            )


if __name__ == "__main__":
    main()
