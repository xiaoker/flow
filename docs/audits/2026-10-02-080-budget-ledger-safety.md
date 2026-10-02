# 080-RUNNER budget ledger safety prerequisite

Base main: `2c110962befca2b8788638e3688a2e04e1d9dc83` (PR #149).
Scope: existing candidate-side resource accounting; no new control plane.

## Confirmed defects and fixes

1. `mark_running`, `debit` and `snapshot` did not expire reservations themselves.
   An expired reservation could still be reported RUNNING unless another caller
   first swept it. They now expire under the existing RLock; exact expiry is
   terminal UNKNOWN, retaining the conservative hold. A complete late measured
   settlement can reconcile it; execution cannot resume via these methods.
2. `debit` accepted estimated counters although it represents physical observed
   usage. Complete measured counters are now required, consistent with settle.
3. A final receipt could be lower than already observed consumption and erase
   cost. That settlement is rejected without releasing the hold.
4. Adding measured vectors dropped timestamps. The ledger's current-usage sum
   therefore failed finite-TTL admission even with fresh evidence. Sums retain
   the oldest supplied timestamp; missing timestamps remain missing. New local
   settlements cannot renew the freshness of the original account evidence.

Eleven new regression cases cover expiry at/after the boundary for RESERVED and
RUNNING, snapshot without sweep, retained capacity, estimated debit rejection,
lower final receipt rejection before/after UNKNOWN, late measured reconciliation,
finite TTL admission/staleness and missing timestamp preservation. No existing
assertions or quota limits are weakened.

## Boundaries and review

The existing `BudgetLedger` remains an in-memory reference, not a shared durable
account ledger. These changes do not authorize live execution, implement R2
lifecycle deletion, provide account telemetry or complete resource admission.
Actual cross-job reservations, lifecycle/restore evidence, full-task cost,
remaining stage adapters and separately authorized canary remain pending.

Manual review uses `docs/code-review-checklist.md`: changes stay in one business
module; unknown is retained, methods share the existing lock, no network/D1/R2
or credential operations, no formulas, dates, workflow, flags or pointer changes.
Original `reports/phase0/latest-*`, `memory/` and other user changes are untouched.

## Verification and merge effects

Full Python pytest: 597 passed; web npm test (includes build): 166 passed;
npm run lint and diff check passed. Hosted results are recorded in the PR.
Locked dependency audit still reports 24
existing vulnerabilities; dependencies are unchanged.

PR/main CI consumes Actions minutes. This Python/tests/docs-only diff does not
match the Worker deploy workflow's web/workflow path filter. No migration or
production deployment occurs on merge. Rollback is a code revert with no stored
state/schema compatibility change. Stop after CI for merge authorization.
