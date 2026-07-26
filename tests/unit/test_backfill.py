from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

import pandas as pd

from trading_desk import backfill
from datetime import date

import requests

from trading_desk.backfill import (
    EMPTY_CONNECT_COLUMNS,
    _discover_sse_trading_dates,
    _publish,
    _snapshot_for_date,
)


def test_snapshot_for_date_resolves_relative_work_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    dates = ["2024-07-31", "2024-08-01"]
    etf = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "instrument_code": "510300",
                "instrument_name": "沪深300ETF",
                "shares": shares,
                "nav": 1.0,
            }
            for trade_date, shares in zip(dates, [100_000_000, 110_000_000])
        ]
    )
    margin = pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "financing_balance_yuan": balance,
            }
            for trade_date, balance in zip(dates, [1_000_000_000, 1_100_000_000])
        ]
    )

    snapshot = _snapshot_for_date(
        trade_dates=dates,
        sse_etf=etf,
        sse_margin=margin,
        szse_margin=margin,
        connect=pd.DataFrame(columns=EMPTY_CONNECT_COLUMNS),
        observed_at="2024-08-01T18:00:00+08:00",
        connect_closed=True,
        work_root=Path("data/backfill"),
    )

    assert snapshot["asOf"] == "2024-08-01"
    assert snapshot["mode"] == "官方历史归档"


def test_publish_recovers_from_extended_transient_503_window(monkeypatch) -> None:
    failed = Mock()
    failed.ok = False
    failed.status_code = 503
    failed.text = '{"error":"storage_unavailable"}'
    succeeded = Mock()
    succeeded.ok = True
    succeeded.json.return_value = {"ok": True}
    post = Mock(side_effect=[failed] * 5 + [succeeded])
    sleep = Mock()
    monkeypatch.setattr(backfill.requests, "post", post)
    monkeypatch.setattr(backfill.time, "sleep", sleep)

    result = _publish(
        url="https://flow.example/api/v1/admin/history-import",
        bundle={"version": 1},
        ingest_key="test-key",
        access_client_id="test-client",
        access_client_secret="test-secret",
    )

    assert result == {"ok": True}
    assert post.call_count == 6
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8, 16]


def test_calendar_discovery_uses_yearly_chunks_and_retries_403(monkeypatch) -> None:
    adapters = Mock()
    forbidden = requests.HTTPError("403")
    adapters.fetch_sse_margin.side_effect = [
        forbidden,
        (
            pd.DataFrame({"trade_date": ["2023-12-29"]}),
            Mock(),
        ),
        (
            pd.DataFrame({"trade_date": ["2024-01-02", "2024-12-31"]}),
            Mock(),
        ),
        (
            pd.DataFrame({"trade_date": ["2025-01-02"]}),
            Mock(),
        ),
    ]
    sleep = Mock()
    monkeypatch.setattr(backfill.time, "sleep", sleep)

    dates = _discover_sse_trading_dates(
        adapters, date(2023, 11, 28), date(2025, 4, 30)
    )

    assert dates == ["2023-12-29", "2024-01-02", "2024-12-31", "2025-01-02"]
    assert adapters.fetch_sse_margin.call_args_list == [
        call("2023-11-28", "2023-12-31"),
        call("2023-11-28", "2023-12-31"),
        call("2024-01-01", "2024-12-31"),
        call("2025-01-01", "2025-04-30"),
    ]
    sleep.assert_called_once_with(1)
