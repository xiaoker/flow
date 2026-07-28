import json
from pathlib import Path

from scripts.rebuild_remote_market_history import _record, _write_sql


def test_rebuild_extracts_only_the_snapshot_date(tmp_path: Path) -> None:
    snapshot = {
        "asOf": "2026-07-14",
        "observedAt": "2026-07-15T01:15:00Z",
        "marginSeries": [
            {"date": "07-13", "balance": 100.0},
            {
                "date": "07-14",
                "balance": 101.0,
                "change": 1.0,
                "sseBalance": 51.0,
                "szseBalance": 50.0,
            },
        ],
        "connectSeries": [
            {
                "date": "07-14",
                "southboundNet": 2.0,
                "southboundTurnover": 10.0,
                "southboundSseNet": 1.2,
                "southboundSzseNet": 0.8,
                "northboundTurnover": 20.0,
                "northboundEtfTurnover": 3.0,
                "northboundTradeCount": 100,
            }
        ],
    }
    path = tmp_path / "2026-07-14.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    values, margin_available, connect_available = _record(
        path,
        {"snapshot_sha256": "a" * 64},
    )

    assert values[0] == "2026-07-14"
    assert values[1] == 101.0
    assert values[9] == 2.0
    assert values[18] == "a" * 64
    assert margin_available is True
    assert connect_available is True


def test_rebuild_keeps_expired_hkex_day_missing(tmp_path: Path) -> None:
    snapshot = {
        "asOf": "2024-08-01",
        "observedAt": "2026-07-22T01:00:00Z",
        "marginSeries": [{"date": "08-01", "balance": 100.0}],
        "connectSeries": [{"date": "07-31", "southboundNet": 9.0}],
    }
    path = tmp_path / "2024-08-01.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    values, margin_available, connect_available = _record(
        path,
        {"snapshot_sha256": "b" * 64},
    )

    assert margin_available is True
    assert connect_available is False
    assert values[9] is None
    assert values[17] == 0

    sql_path = tmp_path / "history.sql"
    _write_sql([values], sql_path)
    sql = sql_path.read_text(encoding="utf-8")
    assert "INSERT INTO market_history_daily" in sql
    assert "ON CONFLICT(trade_date) DO UPDATE" in sql
