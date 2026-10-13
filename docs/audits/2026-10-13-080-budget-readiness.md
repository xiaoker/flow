# 080 candidate budget readiness prerequisite

Scope: controlled initialization/refresh of the existing `flow-account-v1` row,
not task metering, production initialization or canary acceptance. Prior bounded
production reads established no account row and a COMPLETED source lease (D1
read 1/write 0). No production mutation in this code delivery.

## Existing administrative contract

POST `/api/v1/admin/pipeline-window/budget` using existing INGEST authentication
and Cloudflare Access. Opt-in `account_reconcile` administers the existing account;
ordinary/recovery operations retain their prior lease/approval fences. No new
table, migration, workflow, scheduler or credential system.

Exactly six fields: operation=`account_reconcile`, stable 64-hex operationId,
expectedRevision=null for absent-account creation or exact current integer for
refresh, explicit boolean dryRun, evidence, and evidenceReference.

Evidence is the existing seven-dimension measured account Vector, exact units,
timestamps and scope=account. Unknown, stale, future, estimated or regressing
cumulative evidence rejects. Each metric has exactly value/unit/basis/scope/
measuredAt. Reference is `official-usage-sha256:<64 lowercase hex>` for the retained
complete official export. This is administrator provenance attestation, NOT
independent server verification of the export or a provider signature. Hashing
invented JSON is not valid evidence. Operator validation is required before apply;
no credentials/full exports belong in request logs.

Caller cannot supply caps, epochs, TTLs, reservations, receipts or settled counters.
Policy remains config/resource-budget.json. Use dryRun=true first: proposal digest,
no remote writes and executionAllowed=false. Apply requires separate explicit
production authority. INSERT ON CONFLICT DO NOTHING and refresh CAS allow one
winner. Preserve original operationId/revision on ambiguous response; exact replay
is read-only and grants no execution. Do not replace uncertainty with manual SQL.

Refresh and rollover retain every reservation, UNKNOWN/pending state, receipt and
cumulative settled charge; overdue holds are not released. Same-period cumulative
provider counters cannot regress; measured storage occupancy may fall. Preserving
old charges can conservatively over-count after rollover. Individual hold
reconciliation is out of scope. Receipt/state capacity fails closed, no pruning.
Missing D1 meta remains null, never logical SQLite counts passed as provider usage.

## R2 policy and units

Standard R2 official free monthly caps: 1,000,000 Class A / 10,000,000 Class B:
https://developers.cloudflare.com/r2/pricing/
Local conservative hard caps: 500,000 / 5,000,000; minimum policy/prior lower caps
apply. These are ceilings, NOT remaining quota. Missing caps remain UNKNOWN.
Standard free allowance does not cover Infrequent Access. R2 A/B and Actions use
month-to-date evidence; D1 and Workers use UTC day; R2 bytes is current measured
account occupancy. Other projects in the same account also count.

## Execution order and actual merge effects

1. Verify code, complete Python and Web checks, deliver one PR/initial CI under
   bounded authorization. Existing Flow CI jobs use 10/15-minute timeouts; Branch
   freshness also runs and lacks an explicit timeout. Official capacity UNKNOWN.
2. Merge requires explicit authority: Web-path merge automatically triggers main
   CI plus Worker/assets deploy (20-minute timeout), applies ALL pending D1
   migrations, then verifies merge SHA. This PR adds no migration. Current pending
   status is not presumed from PR #182's prior "No migrations to apply" evidence.
3. Validate complete official usage export and prepare live dry-run. Missing facts
   are UNKNOWN, not zero. Separately authorize one account apply and bounded
   receipt/revision readback; preserve operationId on lost response.
4. Obtain legitimate lease/approved recovery scope and the one exact source-bound
   candidate authorization package. This patch does not claim, approve, dispatch,
   collect, activate, change flags or grant that authority.
5. Candidate STAGED versus accounting UNKNOWN remains separate. No fake
   settled/COMPLETED result, UNKNOWN hold release or automatic reader switch.

Tests use real local SQLite with explicitly synthetic meta, not production usage.
Earlier /private/tmp checkout was removed before delivery; implementation restored
from the recorded patches to a persistent managed worktree. Fresh verification:
Web `npm run lint`, `npm test` production build and 228 tests passed, then
root `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -o addopts='' -q --tb=short`
passed 1111 tests in 51.50s. Narrow budget/reconciliation suite passed 31 tests.
Independent read-only coverage/plan review found no safety/scope blocker; added
month rollover, cap preservation, existing-account dry-run, unknown D1 meta,
storage failure and receipt-capacity tests. No fabricated coverage percentage.
Existing lockfile was not changed; npm ci reports dependency audit warnings that
were not addressed through unrelated upgrades in this scoped change.
