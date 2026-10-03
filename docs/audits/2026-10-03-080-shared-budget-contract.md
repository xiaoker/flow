# 080 shared budget implementation contract

Status: candidate implementation; not production-wired or production-authorized.
Base: `7ff01e17eda95e57a4da8eff363b5e10a121f1f2` (PR #152).

## Boundary

The local SQLite journal protects processes sharing one file. It cannot protect
GitHub and local executors sharing a Cloudflare account. Do not advertise it as
distributed admission or wire it into production scheduling.

Use the existing Worker, D1, administrator authentication and pipeline_window
lease. Do not introduce another service, scheduler, lease or billing platform.
The shared adapter must preserve the existing BudgetLedger transitions, including
full holds for UNKNOWN, TIMEOUT and OVER_BUDGET. A generic opaque CAS endpoint
without validated budget transitions is not sufficient.

## Required atomic operation

Every mutation binds account scope, accounting contract, evidence epoch,
idempotency operation ID, expected revision and pipeline owner/attempt. Validate
the current unexpired ordinary pipeline lease before granting permission.
Commit the bounded account state transition before returning allowance. A stale
revision cannot grant permission; the client may refresh and recompute within an
explicit retry cap. Lost responses are resolved by the same operation ID, not a
second spend. Owner takeover cannot erase the predecessor's outstanding holds.

The implementation must validate reserve, authorize_step, measured debit and
settlement, rather than accept arbitrary client-authored balance replacement.
Python/Worker parity fixtures must exercise the same transitions and denials.
No successful fixture constitutes official account telemetry.

## Evidence and period rollover

All seven dimensions retain measured/estimated/unknown, units, timestamps and
scope. Limits are not remaining-balance evidence. Existing missing R2 operation
caps remain UNKNOWN; do not invent caps or require a fabricated usage JSON.

Daily D1/Worker and monthly Actions periods differ. A fresh evidence export is
not permission to clear active holds. Period rollover must reconcile unresolved
reservations explicitly; until that exists, fail closed rather than silently
initialize an empty new epoch. Account-wide external use remains a separate
telemetry uncertainty, even with correct application reservations.

## Implementation and acceptance

One candidate-only increment must include additive schema, guarded Worker
operations, bounded Python adapter and behavior tests. No default production
caller, flag changes, migration execution, collection or pointer mutation.
Indexed single-account lookup and conditional revision updates only; bound state
size, reservation count, response bytes, calls and retries. Report actual D1 meta;
never substitute logical rows for physical rows.

Required tests: concurrent admission does not oversell; independent clients see
committed allowance; replay after lost response grants no second spend; stale
owner/attempt and expired lease cannot authorize; unknown outcomes retain holds;
settlement cannot erase observed cost; insufficient/stale/unknown evidence denies;
config/contract mismatch denies; rollback/commit failure returns no allowance;
period change cannot reset unresolved spend; bounded capacity fails closed.

Web changes will trigger the existing merge deployment workflow, including ALL
pending migrations, not just a new table. Before merge, disclose exact additive
SQL and deployment side effects. Do not apply remote migrations in the code PR.

## Remaining 080 work

Shared reservations alone do not complete 080-RUNNER. Checkpoint lifecycle and
large-artifact handling, remaining real stage adapters, recovery integration,
whole-task cost evidence and an explicitly authorized real canary/rollback remain.

## Delivered candidate and verified boundaries

- Additive migration `0015_shared_budget_account` creates one bounded account row
  store. No seed, reset, rollover, evidence refresh or remote migration was run.
- Existing admin authentication and ordinary pipeline owner/attempt/target/lease
  fence guard GET metadata and validated POST transitions. Recovery rows are
  deliberately unsupported in this increment. Conditional revision SQL fences
  the lease again at commit; the client cannot replace balances or policy.
- Seven-dimensional admission clamps stored caps against repository policy;
  missing R2 operation limits remain UNKNOWN. Missing/stale account measurements
  deny admission. UNKNOWN/TIMEOUT/expired holds and over-budget evidence persist.
- Python uses bounded, no-retry requests and an explicit same-ID reconciliation.
  Replayed or ambiguous step authorization never grants another external effect.
  A foreground deadline denies allowance even if a daemon request later finishes;
  that request cannot grant permission, and another request is blocked while it
  remains in flight. Unobserved D1 meta remains sticky UNKNOWN.
- State limits: 128 KiB, 64 reservations, 128 receipts. Request/response limits:
  16 KiB, 64 chunks, at most 32 client calls, 20-second foreground deadline.
  Capacity exhaustion fails closed; no automatic pruning erases audit holds.
- Concurrency tests use 20 independent local SQLite connections and one shared
  file: exactly one admission wins, 19 lose the CAS. This proves fixture behavior,
  not production D1 usage or actual account availability. A localhost Python-to-
  Worker fixture also verifies cross-client denial. Synthetic test D1 meta is not
  reported as measured production consumption.
- Remaining prerequisites include evidence provisioning/reconciliation and period
  rollover, full-task/control-plane cost bounds, lifecycle, recovery and live
  acceptance. This PR does not enable any production caller or complete 080.

Merge side effects are recorded in
`2026-10-03-080-shared-budget-merge-side-effects.json`. Production pending
migrations remain UNKNOWN; merge can apply all pending migrations, not only 0015.

## Local verification (2026-10-03)

- `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q`:
  642 tests passed. Localhost socket tests required the isolated test run outside
  the filesystem sandbox; no production network request was made by these tests.
- `npm test`: build and 177 tests passed; `npm run lint` and `npm run build` passed.
- `git diff --check`: passed. Original dirty-worktree file list unchanged.
- Independent review identified ambiguous-request telemetry and slow-response
  deadline gaps; both were fixed with regression tests. Hosted CI remains pending
  until PR creation. Build chunk-size warnings remain; dependency remediation
  is outside this candidate protocol and no dependency lock was changed.
