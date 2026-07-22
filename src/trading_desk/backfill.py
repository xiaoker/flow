from __future__ import annotations

import json
import time
from collections import deque
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .adapters import OfficialMarketAdapters
from .dashboard import build_dashboard_snapshot
from .history_bundle import build_history_bundle
from .http import build_session
from .models import FetchArtifact
from .quality import validate_connect_summary, validate_etf, validate_margin
from .storage import new_run_id, now_iso, write_json


LOOKBACK_TRADING_DAYS = 260

EMPTY_SZSE_ETF_COLUMNS = [
    "trade_date",
    "market",
    "instrument_code",
    "instrument_name",
    "etf_type",
    "investment_type",
    "shares",
    "nav",
]

EMPTY_CONNECT_COLUMNS = [
    "trade_date",
    "market",
    "direction",
    "currency",
    "total_turnover",
    "buy_turnover",
    "sell_turnover",
    "net_buy",
    "total_trade_count",
    "etf_turnover",
]


def _assert_frame_quality(
    *,
    trade_date: str,
    sse_etf: pd.DataFrame,
    sse_margin: pd.DataFrame,
    szse_margin: pd.DataFrame,
    connect: pd.DataFrame,
) -> bool:
    failures = [
        finding
        for finding in (
            validate_etf(sse_etf, "sse_etf")
            + validate_margin(sse_margin, "sse_margin")
            + validate_margin(szse_margin, "szse_margin")
            + ([] if connect.empty else validate_connect_summary(connect))
        )
        if finding.status == "fail"
    ]
    if failures:
        detail = "; ".join(f"{item.check_id}:{item.message}" for item in failures)
        raise ValueError(f"quality gate failed for {trade_date}: {detail}")
    for name, frame in (
        ("sse_etf", sse_etf),
        ("sse_margin", sse_margin),
        ("szse_margin", szse_margin),
    ):
        observed = sorted(frame["trade_date"].dropna().astype(str).unique().tolist())
        if observed != [trade_date]:
            raise ValueError(f"{name} date mismatch for {trade_date}: {observed}")
    return connect.empty


def _publish(
    *,
    url: str,
    bundle: dict[str, Any],
    ingest_key: str,
    access_client_id: str,
    access_client_secret: str,
) -> dict[str, Any]:
    body = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {ingest_key}",
        "CF-Access-Client-Id": access_client_id,
        "CF-Access-Client-Secret": access_client_secret,
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = requests.post(url, data=body, headers=headers, timeout=(10, 90))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("history import did not return a JSON object")
            return result
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2**attempt)
    raise RuntimeError(f"history import failed after retries: {last_error}")


def _existing_dates(
    *,
    url: str,
    access_client_id: str,
    access_client_secret: str,
) -> set[str]:
    response = requests.get(
        url,
        headers={
            "CF-Access-Client-Id": access_client_id,
            "CF-Access-Client-Secret": access_client_secret,
        },
        timeout=(10, 45),
    )
    response.raise_for_status()
    payload = response.json()
    return {
        value
        for value in payload.get("dates", [])
        if isinstance(value, str)
    }


def _concat(frames: deque[pd.DataFrame], columns: list[str] | None = None) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    if nonempty:
        return pd.concat(nonempty, ignore_index=True)
    return pd.DataFrame(columns=columns or [])


def _snapshot_for_date(
    *,
    trade_dates: list[str],
    sse_etf: pd.DataFrame,
    sse_margin: pd.DataFrame,
    szse_margin: pd.DataFrame,
    connect: pd.DataFrame,
    observed_at: str,
    connect_closed: bool,
    work_root: Path,
) -> dict[str, Any]:
    combined = work_root / "snapshot-input"
    combined.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    frames = {
        "sse_etf": sse_etf,
        "szse_etf": pd.DataFrame(columns=EMPTY_SZSE_ETF_COLUMNS),
        "sse_margin": sse_margin,
        "szse_margin": szse_margin,
        "stock_connect_summary": connect,
    }
    for name, frame in frames.items():
        path = combined / f"{name}.csv"
        frame.to_csv(path, index=False, encoding="utf-8")
        paths[name] = str(path)

    manifest = {
        "requested_dates": trade_dates,
        "finished_at": observed_at,
        "combined_paths": paths,
    }
    warning_count = 1 + int(connect_closed)
    quality = {
        "counts": {"pass": 8, "warn": warning_count, "fail": 0},
        "phase0_gate_status": "conditional_pass",
    }
    manifest_path = combined / "manifest.json"
    quality_path = combined / "quality.json"
    write_json(manifest_path, manifest)
    write_json(quality_path, quality)
    snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=Path("/"),
    )
    snapshot["mode"] = "官方历史归档"
    return snapshot


def run_history_backfill(
    *,
    start_date: date,
    end_date: date,
    data_root: Path,
    report_path: Path,
    publish_url: str,
    dates_url: str,
    ingest_key: str,
    access_client_id: str,
    access_client_secret: str,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end date must not be before start date")
    run_id = f"history-{new_run_id()}"
    started_at = now_iso()
    adapters = OfficialMarketAdapters(
        session=build_session(), data_root=data_root, run_id=run_id
    )
    discovery_start = start_date - timedelta(days=400)
    calendar_frame, _ = adapters.fetch_sse_margin(
        discovery_start.isoformat(), end_date.isoformat()
    )
    all_dates = sorted(
        calendar_frame["trade_date"].dropna().astype(str).unique().tolist()
    )
    targets = [value for value in all_dates if start_date.isoformat() <= value <= end_date.isoformat()]
    if not targets:
        raise RuntimeError("official SSE calendar did not contain target trading dates")

    existing = _existing_dates(
        url=dates_url,
        access_client_id=access_client_id,
        access_client_secret=access_client_secret,
    )
    pending = [value for value in targets if value not in existing]
    if not pending:
        result = {
            "runId": run_id,
            "status": "already_complete",
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "targetDates": len(targets),
            "publishedDates": 0,
            "skippedDates": len(targets),
        }
        write_json(report_path, result)
        return result

    first_pending_index = all_dates.index(pending[0])
    has_prior_archived_target = any(value in existing for value in targets if value < pending[0])
    context_days = LOOKBACK_TRADING_DAYS + 1 if has_prior_archived_target else 5
    context_start_index = max(0, first_pending_index - context_days)
    collection_dates = [
        value
        for value in all_dates[context_start_index:]
        if value <= end_date.isoformat()
    ]
    target_set = set(targets)
    pending_set = set(pending)
    etf_window: deque[pd.DataFrame] = deque(maxlen=LOOKBACK_TRADING_DAYS + 2)
    sse_margin_window: deque[pd.DataFrame] = deque(maxlen=LOOKBACK_TRADING_DAYS + 2)
    szse_margin_window: deque[pd.DataFrame] = deque(maxlen=LOOKBACK_TRADING_DAYS + 2)
    connect_window: deque[pd.DataFrame] = deque(maxlen=LOOKBACK_TRADING_DAYS + 2)
    date_window: deque[str] = deque(maxlen=LOOKBACK_TRADING_DAYS + 2)
    published: list[str] = []
    closed_connect_dates: list[str] = []

    for index, trade_date in enumerate(collection_dates, start=1):
        sse_etf, etf_artifact = adapters.fetch_sse_etf(trade_date)
        sse_margin, sse_margin_artifact = adapters.fetch_sse_margin(trade_date, trade_date)
        szse_margin, szse_margin_artifact = adapters.fetch_szse_margin(trade_date)
        connect, _, connect_artifact = adapters.fetch_hkex_connect(trade_date)
        connect_closed = _assert_frame_quality(
            trade_date=trade_date,
            sse_etf=sse_etf,
            sse_margin=sse_margin,
            szse_margin=szse_margin,
            connect=connect,
        )
        if connect_closed:
            closed_connect_dates.append(trade_date)
            connect = pd.DataFrame(columns=EMPTY_CONNECT_COLUMNS)
        etf_window.append(sse_etf)
        sse_margin_window.append(sse_margin)
        szse_margin_window.append(szse_margin)
        connect_window.append(connect)
        date_window.append(trade_date)

        if trade_date not in target_set or trade_date not in pending_set:
            continue
        snapshot = _snapshot_for_date(
            trade_dates=list(date_window),
            sse_etf=_concat(etf_window),
            sse_margin=_concat(sse_margin_window),
            szse_margin=_concat(szse_margin_window),
            connect=_concat(connect_window, EMPTY_CONNECT_COLUMNS),
            observed_at=now_iso(),
            connect_closed=connect_closed,
            work_root=data_root,
        )
        artifacts: list[FetchArtifact] = [
            etf_artifact,
            sse_margin_artifact,
            szse_margin_artifact,
            connect_artifact,
        ]
        bundle = build_history_bundle(
            snapshot=snapshot,
            artifacts=artifacts,
            run_id=run_id,
            started_at=started_at,
            finished_at=now_iso(),
            workspace_root=Path("/"),
        )
        _publish(
            url=publish_url,
            bundle=bundle,
            ingest_key=ingest_key,
            access_client_id=access_client_id,
            access_client_secret=access_client_secret,
        )
        published.append(trade_date)
        progress = {
            "runId": run_id,
            "status": "running",
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "targetDates": len(targets),
            "pendingDatesAtStart": len(pending),
            "publishedDates": len(published),
            "lastPublished": trade_date,
            "closedConnectDates": closed_connect_dates,
        }
        write_json(report_path, progress)
        print(
            json.dumps(
                {
                    "progress": f"{len(published)}/{len(pending)}",
                    "tradeDate": trade_date,
                    "sourceLoop": f"{index}/{len(collection_dates)}",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(0.05)

    result = {
        "runId": run_id,
        "status": "complete",
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "targetDates": len(targets),
        "publishedDates": len(published),
        "skippedDates": len(targets) - len(pending),
        "firstPublished": published[0] if published else None,
        "lastPublished": published[-1] if published else None,
        "closedConnectDates": closed_connect_dates,
        "finishedAt": now_iso(),
    }
    write_json(report_path, result)
    return result
