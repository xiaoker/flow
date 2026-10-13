# 080-CANARY prerequisite: archive-first and bounded budget evidence

Status: local implementation; no production execution authorized or performed.

Delivery authorization: one independent PR and one initial necessary CI, not merge or deployment. Final ship re-verification passed Web lint/build plus 240 tests and all 1111 Python tests in 50.29s. Independent coverage/plan audits found no runtime blocker; corrected one false-positive settlement assertion and added measured-only debit rejection. No separate report-only push or helper PR.

## Contract correction

The roadmap permits a proved conservative remaining lower bound, not only seven exact billing counters. Explicit `flow-account-evidence-v3` extends the existing account reconciliation endpoint and row. It accepts exact v2 metrics or an attested interval with `precision=conservative_upper`, `basis=estimated`, integer `lowerBound <= value`, and `boundReference` equal to the request's official usage SHA reference. `value` is the consumption upper bound, never a measured point estimate.

Admission requires, independently for all seven dimensions:

`usageUpperBound + settled + held + taskCostUpperBound <= configuredCap`.

Existing units, provider periods, observation freshness, authentication, CAS, receipts, caps and UNKNOWN occupancy remain enforced. Refinement intersects the cumulative lower floor with previous evidence. It cannot erase charges, reset holds or downgrade the evidence contract. Debit and settlement still require measured actuals.

This remains administrator attestation. A hash is not a provider signature and does not establish a valid interval by itself. Unknown values, arbitrary estimates and rounded dashboard labels without proven rounding semantics are not acceptable bounds. `measuredAt` must describe the actual evidence cutoff, not the time an old export was copied.

## Archive reuse evidence

Read-only verification on 2026-10-13 found artifact 11721908335, `market-data-collection-source-38193100238`, unexpired until 2026-10-15T01:48:47Z. Its confirmed snapshot has `asOf=2026-10-09` and SHA256 `908ff478a6fced6ae044f9ff29f2e20c1b3d7fd191b7f76f3ada73abe9b18da1`. The existing bounded artifact reader verified metadata, archive limits, snapshot hash, date and publication status. Collection requests: zero. This proves one reusable day, not complete historical coverage or execution authority.

A real candidate must obtain its own legitimate task identity, lease, approvals and budget reservation. It must not borrow the completed source task's lease. Reuse archived official inputs first; collect only independently proven missing inputs.

## Scope and next gate

No migration, workflow, formula, classification, feature flag or active pointer changes. No production reconciliation/apply, claim, recovery registration, dispatch, backfill or canary occurred. Original `reports/phase0/latest-*`, `memory/` and user changes remain untouched.

Merge may trigger the existing main CI and automatic Worker deployment, including its pending-migration check. This code introduces no migration. Merge/deployment and a later real bounded candidate are separate authorization gates.

Local regression tests exercise SQLite-backed dry-run/apply, exact/bounded mixed evidence, all seven boundary admissions, proof/shape/freshness rejection, interval refinement, no downgrade, preserved UNKNOWN occupancy and measured-only settlement. Verification on this worktree: `node --experimental-strip-types --test web/tests/pipeline-budget-reconciliation.test.mjs` passed 19; `npm run lint && npm test` in web passed lint/build and 240 tests; `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -o addopts='' -q --tb=short` passed 1111 tests in 50.89s. These are local results, not production validation.
