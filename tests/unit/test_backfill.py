from __future__ import annotations

from pathlib import Path

import pandas as pd

from trading_desk.backfill import EMPTY_CONNECT_COLUMNS, _snapshot_for_date


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
