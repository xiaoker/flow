# 080-RUNNER candidate checkpoint pre-spend allowance

Base: `ef999521db5fed3271b87679f2f6e6b81b2e7878` (PR #150 merged;
main CI `37030459135` passed). One purpose: connect existing candidate resource
reservation to checkpoint I/O before sending requests.

## Implementation

- `BudgetLedger.authorize_step` atomically consumes a complete step upper bound
  within an existing reservation, using the same lock and expiry semantics.
  Concurrent clients cannot each spend the same remaining allowance.
- The commitment is reported as `authorizedUpperBound`, separate from measured
  `consumed`. Failed/ambiguous requests do not automatically refund it. The full
  reservation remains held until existing complete measured settlement or UNKNOWN.
- Checkpoint transport requires an existing ledger reservation, task identity and
  complete per-method cost upper bounds before lease confirmation or HTTP I/O.
  Missing/unknown cost, nonexistent/expired/terminal reservation, insufficient
  allowance and understated basic operation floors fail closed.
- Each ambiguity GET consumes its own allowance. No second POST is introduced.
  Local per-client request cap and shared in-process allowance both apply.
- Costs must cover lease confirmations/possible renewals, server R2 operations
  and execution cost. Basic operation floors are not a complete task-cost proof.
  No numerical production cost or remaining account balance is manufactured.

## Boundaries

This reuses the reference ledger; it is **not shared durable cross-job admission**.
Transport evidence explicitly says `sharedDurableReservation=false`. Two clients
can share one ledger inside a process; separate GitHub jobs or local machines
cannot use independent ledgers as proof of account-wide exclusivity.

No production CLI/workflow is enabled. The gate covers checkpoint I/O, not all
runner/source/lease traffic. A future live package still requires persistent shared
reservation, verified telemetry or confirmed budget lower bound, full-task cost,
checkpoint lifecycle/restore evidence, real artifact sizing, remaining adapters
and authorization. No retention policy or deletion is implemented here.

## Tests and review

Ten new regression cases: twenty simultaneous step requests have one winner;
expired/terminal/unknown allowance cannot continue; estimated commitments do not
become physical measurements; missing/understated/unreserved budget prevents lease
or object HTTP; ambiguous upload retains commitment and cannot read back without
budget; two actual transport clients share one allowance and send only one request.
All use local fixtures, not provider evidence. Existing source-resume and transport
integrity tests continue unchanged in purpose.

Full Python: 607 passed; Web npm test (includes build): 166 passed;
npm run lint and git diff --check passed. Hosted results are recorded in the PR. Locked dependencies
retain 24 known audit findings; this PR does not change dependencies.
Manual review follows `docs/code-review-checklist.md`: no secrets logged, bounded
operations and existing lock only, no schema/formula/date/production-read changes.

## Merge effects / continuation

Only two business modules, tests and docs change. PR/main CI consumes Actions
minutes. No web/workflow paths match Worker auto-deploy, no migration or production
task/flag/pointer changes. Rollback is a code revert with no remote data effects.
Original protected reports, memory and user changes remain untouched.

After CI stop for merge authorization. Then continue the missing persistent budget
and lifecycle prerequisites from latest main; do not mark 080-RUNNER complete or
run a canary on the strength of this in-process fixture.
