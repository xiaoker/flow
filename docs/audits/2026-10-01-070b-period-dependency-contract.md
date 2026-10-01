# 070-B ETF period dependency planner

Status: candidate-only implementation contract. This is not a production
history-repair run, a candidate upload, or a production-read-path change.

## Purpose

The existing ETF terminal materializers already sum official daily share
changes and same-date-NAV cash estimates. The missing piece is a bounded,
reproducible dependency plan that proves which official sessions, market facts,
formulas, classifications, and model nodes each output depends on. The planner
must not treat an observed snapshot sequence as an official calendar.

The implementation is in `src/trading_desk/etf_period_dependencies.py`; the
offline entry point is `scripts/plan_etf_period_rebuild.py`.

## Input evidence contract

The input JSON contains:

- `calendar`: `sourceId=sse_official_trading_calendar`, a SHA-256 of the
  immutable official source artifact, a `dates` list of strictly increasing
  ISO business dates, and `datesSha256` over that canonical date list. Dates
  must come from official source evidence (the existing SSE official
  trade-date series), not weekdays or observed release dates.
- `targetDates`: explicit official sessions to plan, at most 280.
- `sourceEvidence`: at most 1,066 date × market rows for SSE/SZSE. Each row
  records raw and normalized stage status, hashes, byte counts, normalized
  source-to-raw hash linkage, share row count, and same-date NAV-aligned rows.
- `existingModels`: at most 11,200 bounded model descriptors identifying
  daily-change or period models by date, period, metric, sample scope, status,
  and input fingerprint.
- Optional `periods`, `classificationVersion`, and `formulaVersions`. Omitted
  formula versions use the currently approved formulas; an explicit malformed
  or empty version is rejected.

Raw/normalized evidence that is absent, unknown, empty, or hash-mismatched is
not promoted to a usable fact. The plan names the exact date, market, stage,
and safe next action. A partial same-date NAV sample is `BUILD_PARTIAL` with
its observed/expected row counts; zero aligned NAV blocks that amount node but
does not block share-change nodes.

## Window and invalidation rules

- Fixed periods use exactly N official daily changes and N+1 official
  snapshots: 1, 3, 5, 10, 21, 63, 126, or 252 changes plus the immediately
  preceding official baseline.
- YTD starts at the first official session of the target year and requires
  the immediately preceding official session as its baseline.
- A missing calendar baseline is `BLOCKED_CALENDAR`; the planner does not
  guess the missing date. Weekends and holidays are absent only because the
  supplied authoritative calendar excludes them.
- Daily-change fingerprints bind the exact previous/current date pair, raw
  and normalized source identities, formula, classification, and calendar.
  Period fingerprints bind their exact daily model dependencies. Matching
  existing models are reused; only missing/stale downstream nodes are marked
  `BUILD` or `BUILD_PARTIAL`.
- The overall plan hash binds the full supplied calendar artifact and selected
  evidence. Individual model fingerprints bind only their own official-session
  window; an unrelated calendar correction therefore changes the audit plan
  identity without needlessly invalidating independent models.
- `planHash` is deterministic for equivalent evidence and contract versions;
  no wall-clock timestamp, runner ID, or input ordering is included.

## Bounds and side effects

The planner accepts at most 280 target sessions, 533 official calendar
sessions (280 targets plus up to 252 lookback sessions and a baseline), 1,066
source rows, 11,200 model descriptors, and an 8 MiB JSON input file. Iterable
targets and period selections are capped while being consumed, and the CLI
reads no more than 8 MiB + 1 byte even if the file changes after its size
check. Duplicate JSON object keys are rejected to avoid ambiguous evidence.
It only reads the supplied local file and writes the requested local plan file. It
does not access an exchange, GitHub API, Cloudflare, D1, or R2; it does not
normalize raw artifacts, rebuild model values, upload a candidate, change a
flag, dispatch a workflow, or activate a release. Future build cost remains
`UNKNOWN` until execution telemetry is supplied.

Run from the repository root:

```bash
PYTHONPATH=src:. python scripts/plan_etf_period_rebuild.py \
  --input /path/to/reviewed-evidence.json \
  --output /path/to/etf-period-rebuild-plan.json
```

The output's `executionStatus` is always `NOT_RUN`. `PLAN_READY` means the
listed local model work can be scheduled after separate authorization; it
does not mean those models were built, QA-passed, published, or date-visible.
