# D1 Free Tier PR 0 baseline

This directory records the read-only baseline for
`docs/handoff/2026-08-29-d1-free-tier-long-term-architecture.md`.

- `baseline.json` captures the active release, immutable R2 rollback prefix and
  D1 table counts observed from Wrangler `SELECT` statements on 2026-08-29.
- `query-plan-baseline.json` captures SQLite `EXPLAIN QUERY PLAN` for the
  current `loadOfficialEtfDailyChanges` shape. The default report materializes
  a small fixture but declares the production dimensions (365 trading days,
  1,600 ETFs and five revisions per business key).

Nothing in this directory changes the active pointer, D1 schema, R2 objects or
the production Worker read path.
