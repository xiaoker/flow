"""Run one opt-in legacy/v2 SSE PCF shadow comparison.

The command writes only local evidence.  It does not call an ingest, R2 or
D1 endpoint and its output is never used as a production read model.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

from trading_desk.adapters.official import OfficialMarketAdapters
from trading_desk.http import HttpSettings, build_session
from trading_desk.pcf_capture import build_changed_etf_capture_plan
from trading_desk.pcf_parity import build_pcf_parity_report
from trading_desk.storage import new_run_id


def _normalise_shares(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "SEC_CODE": "instrument_code",
        "基金代码": "instrument_code",
        "SEC_NAME": "instrument_name",
        "基金简称": "instrument_name",
        "TOT_VOL": "shares",
        "当前规模(份)": "shares",
    }
    frame = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns}).copy()
    required = {"trade_date", "instrument_code", "shares"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"shares_missing_columns:{','.join(missing)}")
    if "market" not in frame.columns:
        frame["market"] = "SSE"
    if "instrument_name" not in frame.columns:
        frame["instrument_name"] = frame["instrument_code"].astype(str)
    frame["trade_date"] = frame["trade_date"].astype(str).str[:10]
    frame["instrument_code"] = frame["instrument_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    frame["market"] = frame["market"].astype(str).str.upper()
    # PR4 is intentionally scoped to the SSE PCF source.  A mixed exchange
    # share file must not inflate the required SSE coverage denominator.
    return frame.loc[frame["market"].eq("SSE"), ["trade_date", "market", "instrument_code", "instrument_name", "shares"]]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_shadow(
    *,
    shares_path: Path,
    trade_date: str,
    previous_date: str,
    data_root: Path,
    output: Path,
    max_detail_rows: int = 20_000,
    hard_limit: int = 2_000,
) -> dict:
    shares = _normalise_shares(pd.read_csv(shares_path, dtype=str))
    current = shares.loc[shares["trade_date"].eq(trade_date)].copy()
    previous = shares.loc[shares["trade_date"].eq(previous_date)].copy()
    if current.empty or previous.empty:
        raise ValueError(f"shares_date_missing:{trade_date if current.empty else previous_date}")
    plan = build_changed_etf_capture_plan(
        current,
        previous,
        target_date=trade_date,
        previous_date=previous_date,
        hard_limit=hard_limit,
    )
    run_id = f"pcf-shadow-{trade_date}-{new_run_id()}"
    if plan["status"] in {"BLOCKED_UNIVERSE_ANOMALY", "BLOCKED_QUALITY", "READY_NO_CHANGE"}:
        report = build_pcf_parity_report(
            plan=plan,
            trade_date=trade_date,
            previous_date=previous_date,
            run_id=run_id,
            max_detail_rows=max_detail_rows,
        )
        _write_json(output, report)
        return report

    codes = [item["etfCode"] for item in plan["items"] if item.get("required") and item.get("market") == "SSE"]
    data_root.mkdir(parents=True, exist_ok=True)
    adapters = OfficialMarketAdapters(
        build_session(HttpSettings(total_retries=0)),
        data_root,
        run_id,
    )
    started = time.perf_counter()
    # Sequential dual-run is intentional: the official source is the scarce
    # resource.  It avoids turning a parity check into a second burst of
    # concurrent requests while still producing two independently parsed rows.
    legacy_frame, legacy_artifact = adapters.fetch_sse_etf_pcf(
        codes,
        trade_date,
        max_workers=1,
        max_codes=len(codes),
    )
    v2_frame, v2_artifact = adapters.fetch_sse_etf_pcf_v2(codes, trade_date, previous_date)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = build_pcf_parity_report(
        plan=plan,
        trade_date=trade_date,
        previous_date=previous_date,
        legacy_frame=legacy_frame,
        v2_frame=v2_frame,
        legacy_artifact=legacy_artifact,
        v2_artifact=v2_artifact,
        elapsed_ms=elapsed_ms,
        run_id=run_id,
        max_detail_rows=max_detail_rows,
    )
    _write_json(output, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one opt-in SSE ETF PCF shadow parity check")
    parser.add_argument("--shares", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-date", required=True)
    parser.add_argument("--data-root", type=Path, default=Path(".shadow/pcf-v2"))
    parser.add_argument("--output", type=Path, default=Path("reports/pcf-shadow/report.json"))
    parser.add_argument("--max-detail-rows", type=int, default=20_000)
    parser.add_argument("--hard-limit", type=int, default=2_000)
    args = parser.parse_args()
    try:
        report = run_shadow(
            shares_path=args.shares,
            trade_date=args.trade_date,
            previous_date=args.previous_date,
            data_root=args.data_root,
            output=args.output,
            max_detail_rows=args.max_detail_rows,
            hard_limit=args.hard_limit,
        )
    except Exception as exc:
        error = {"schemaVersion": 1, "status": "ERROR", "shadowOnly": True, "error": f"{type(exc).__name__}:{exc}"}
        _write_json(args.output, error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps({"output": str(args.output), "status": report["status"], "tradeDate": report.get("tradeDate")}, ensure_ascii=False))
    if report["status"] in {"FAIL", "ERROR", "BLOCKED_QUALITY", "BLOCKED_UNIVERSE_ANOMALY"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
