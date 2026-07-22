from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import requests

from trading_desk import backfill
from trading_desk.backfill import EMPTY_CONNECT_COLUMNS, _publish, _snapshot_for_date


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
    failed.raise_for_status.side_effect = requests.HTTPError("503")
    succeeded = Mock()
    succeeded.raise_for_status.return_value = None
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
