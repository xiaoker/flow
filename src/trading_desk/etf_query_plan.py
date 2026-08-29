"""Offline fixture and query-plan baseline for the legacy ETF history query.

The production Worker is intentionally not imported here.  PR 0 only needs a
reproducible description of the expensive query and a deterministic SQLite
fixture that can be used to compare plans before a later offline-change
implementation replaces it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import sqlite3
from typing import Any


LEGACY_QUERY = """
WITH current_rows AS (
  SELECT instrument_code, market, shares,
         ROW_NUMBER() OVER (
           PARTITION BY instrument_code, market
           ORDER BY observed_at DESC, release_id DESC
         ) AS row_number
  FROM etf_daily
  WHERE trade_date = ?
), previous_rows AS (
  SELECT instrument_code, market, shares,
         trade_date AS previous_trade_date,
         ROW_NUMBER() OVER (
           PARTITION BY instrument_code, market
           ORDER BY trade_date DESC, observed_at DESC, release_id DESC
         ) AS row_number
  FROM etf_daily
  WHERE trade_date < ?
), expected_previous AS (
  SELECT MAX(trade_date) AS expected_previous_trade_date
  FROM (
    SELECT trade_date FROM etf_daily
    WHERE market = 'SSE' AND trade_date < ?
    UNION
    SELECT trade_date FROM margin_daily
    WHERE market = 'SSE' AND trade_date < ?
    UNION
    SELECT trade_date FROM connect_daily
    WHERE market = 'SSE' AND trade_date < ?
  ) AS official_calendar
)
SELECT current_rows.instrument_code, current_rows.market,
       current_rows.shares AS current_shares,
       previous_rows.shares AS previous_shares,
       previous_rows.previous_trade_date,
       expected_previous.expected_previous_trade_date
FROM current_rows
LEFT JOIN previous_rows
  ON previous_rows.instrument_code = current_rows.instrument_code
 AND previous_rows.market = current_rows.market
 AND previous_rows.row_number = 1
CROSS JOIN expected_previous
WHERE current_rows.row_number = 1
""".strip()


@dataclass(frozen=True)
class EtfQueryPlanFixture:
    """Dimensions for a deterministic legacy ``etf_daily`` fixture."""

    trading_days: int = 365
    etf_count: int = 1_600
    revisions_per_business_key: int = 5

    @property
    def row_count(self) -> int:
        return (
            self.trading_days
            * self.etf_count
            * self.revisions_per_business_key
        )

    def to_dict(self) -> dict[str, int]:
        return {**asdict(self), "expected_rows": self.row_count}


PRODUCTION_SCALE = EtfQueryPlanFixture()


def _business_dates(count: int, *, end: date = date(2026, 8, 28)) -> list[str]:
    if count <= 0:
        raise ValueError("trading_days must be positive")
    values: list[str] = []
    cursor = end
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(values))


def create_fixture_database(
    connection: sqlite3.Connection,
    fixture: EtfQueryPlanFixture,
) -> list[str]:
    """Create the production-shaped tables and materialize ``fixture`` rows.

    The default production dimensions are available to callers, but tests and
    the CLI intentionally materialize a smaller sample unless explicitly
    asked for the full scale.  No network or D1 mutation is involved.
    """

    if fixture.etf_count <= 0 or fixture.revisions_per_business_key <= 0:
        raise ValueError("etf_count and revisions_per_business_key must be positive")
    connection.executescript(
        """
        CREATE TABLE etf_daily (
          release_id TEXT NOT NULL,
          trade_date TEXT NOT NULL,
          market TEXT NOT NULL,
          instrument_code TEXT NOT NULL,
          shares INTEGER NOT NULL,
          observed_at TEXT NOT NULL
        );
        CREATE TABLE margin_daily (trade_date TEXT NOT NULL, market TEXT NOT NULL);
        CREATE TABLE connect_daily (trade_date TEXT NOT NULL, market TEXT NOT NULL);
        CREATE UNIQUE INDEX etf_daily_pk
          ON etf_daily(release_id, trade_date, market, instrument_code);
        CREATE INDEX etf_daily_lookup_idx
          ON etf_daily(trade_date, market, instrument_code);
        CREATE INDEX etf_daily_category_idx
          ON etf_daily(trade_date, market);
        """
    )
    dates = _business_dates(fixture.trading_days)
    connection.executemany(
        "INSERT INTO margin_daily(trade_date, market) VALUES (?, 'SSE')",
        ((trade_date,) for trade_date in dates),
    )
    connection.executemany(
        "INSERT INTO connect_daily(trade_date, market) VALUES (?, 'SSE')",
        ((trade_date,) for trade_date in dates),
    )

    rows: list[tuple[str, str, str, str, int, str]] = []
    for day_index, trade_date in enumerate(dates):
        for etf_index in range(fixture.etf_count):
            market = "SSE" if etf_index % 2 == 0 else "SZSE"
            code = f"{etf_index:06d}"
            for revision in range(fixture.revisions_per_business_key):
                release_id = f"r{day_index:03d}-{etf_index:04d}-{revision:02d}"
                observed_at = f"{trade_date}T{revision:02d}:00:00Z"
                rows.append(
                    (
                        release_id,
                        trade_date,
                        market,
                        code,
                        100_000 + day_index * 10 + revision,
                        observed_at,
                    )
                )
        if len(rows) >= 20_000:
            connection.executemany(
                """
                INSERT INTO etf_daily(
                  release_id, trade_date, market, instrument_code,
                  shares, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            rows.clear()
    if rows:
        connection.executemany(
            """
            INSERT INTO etf_daily(
              release_id, trade_date, market, instrument_code,
              shares, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    connection.commit()
    return dates


def explain_legacy_query(
    connection: sqlite3.Connection,
    as_of: str,
) -> list[dict[str, Any]]:
    """Return SQLite's plan rows for the legacy query without executing it."""

    rows = connection.execute(
        f"EXPLAIN QUERY PLAN {LEGACY_QUERY}",
        (as_of, as_of, as_of, as_of, as_of),
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "parent": int(row[1]),
            "notused": int(row[2]),
            "detail": str(row[3]),
        }
        for row in rows
    ]


def build_query_plan_report(
    fixture: EtfQueryPlanFixture,
    *,
    declared_scale: EtfQueryPlanFixture = PRODUCTION_SCALE,
) -> dict[str, Any]:
    """Build a machine-readable baseline report for PR 0."""

    with sqlite3.connect(":memory:") as connection:
        dates = create_fixture_database(connection, fixture)
        plan = explain_legacy_query(connection, dates[-1])
        etf_rows = int(
            connection.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
        )
        previous_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM etf_daily WHERE trade_date < ?", (dates[-1],)
            ).fetchone()[0]
        )
    details = [item["detail"] for item in plan]
    return {
        "schemaVersion": 1,
        "queryId": "loadOfficialEtfDailyChanges",
        "query": LEGACY_QUERY,
        "fixture": fixture.to_dict(),
        "declaredProductionScale": declared_scale.to_dict(),
        "asOf": dates[-1],
        "materialized": {
            "etfDailyRows": etf_rows,
            "previousWindowRows": previous_rows,
            "tradingDateCount": len(dates),
        },
        "plan": plan,
        "signals": {
            "usesRowNumber": "ROW_NUMBER" in LEGACY_QUERY,
            "hasHistoricalPredicate": "trade_date < ?" in LEGACY_QUERY,
            "referencesEtfDaily": any("etf_daily" in detail for detail in details),
            "estimatedProductionPreviousWindowRows": (
                (declared_scale.trading_days - 1)
                * declared_scale.etf_count
                * declared_scale.revisions_per_business_key
            ),
        },
    }
