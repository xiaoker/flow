# Remote read-only evidence

Executed with Wrangler against D1 database `flow`; all statements were `SELECT` only and reported `changes: 0`, `rows_written: 0`.

```sql
SELECT name
FROM sqlite_master
WHERE type = 'table'
  AND name IN ('active_release', 'release_manifest', 'historical_snapshot', 'market_history_daily')
ORDER BY name;

SELECT slot, release_id, as_of, manifest_sha256, observed_at, activated_at
FROM active_release
WHERE slot = 'dashboard';

SELECT release_id, as_of, status, schema_version, observed_at, created_at
FROM release_manifest
ORDER BY as_of DESC, created_at DESC
LIMIT 5;

SELECT MIN(as_of), MAX(as_of), COUNT(*) FROM historical_snapshot;
SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM market_history_daily;
```

Observed results:

| Check | Result |
| --- | --- |
| Required tables | all four present |
| Active pointer | `dashboard → release:2026-08-13:ff4b307e291c4534` |
| Active `asOf` | `2026-08-13` |
| Active `observedAt` | `2026-08-14T03:17:42.000Z` |
| `historical_snapshot` | 488 rows, `2024-08-01` … `2026-08-13` |
| `market_history_daily` | 488 rows, `2024-08-01` … `2026-08-13` |
| Active `marginSeries` | 3 points |
| Active `connectSeries` | 3 points |

The active pointer and remote data were not changed by PR0.
