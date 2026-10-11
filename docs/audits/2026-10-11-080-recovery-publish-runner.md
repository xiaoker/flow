# 080-RUNNER exact recovery publish adapter admission

Base cf74f033b21b230f79400f019893a06b0f674501 (PR #175).
Deployment 38109879189 verified exact merge build and no pending migrations.

The preceding transport/server contract was not reachable through injected
runner adapters: runner candidate_publication_requests accepted ordinary mode
only. This PR closes that specific publish admission prerequisite. It does not
interpret dashboard/etf_penetration/stock_flow as generic build permission.

Before any authorization callback or state files, recovery publish now requires:
approved exact publish scope; the existing CoreCandidatePublishAdapter (not an
arbitrary effect-declaring replacement); RecoveryCandidatePublicationTransport;
RecoveryPipelineLease shared by transport and RecoverySharedBudgetLedger; exact
adapter execution context and durable task identity. The runner still verifies
live authority afterward. The helper is pure wiring validation, not approval.
Every actual request remains guarded/budgeted and server-fenced by prior code.
Ordinary paths and other effect restrictions remain unchanged.

Tests: full Python 946 passed; Web 212 passed, build and lint passed. Seven new
tests cover exact wiring, wrong guard/context/task/ledger binding/adapter and
runner rejection before callbacks or filesystem writes. These are local wiring
and rejection fixtures, not a complete remote publication or production canary.
No lowered assertion, skipped test, source request or production operation.

Manual checklist review: scope before side effects; same guard/task references;
existing immutable identity and budget protocol; no new permission flags, SQL,
schema or workflow. No product formula/classification/date change. Protected
original reports/latest, memory and user files remain untouched.

Merge side effects: main CI only; this diff does not match web/** or deployment
workflow path filters. No Worker deployment or migration is triggered by this PR.
No CLI/production registration, candidate upload, active pointer, recovery flag,
canary or collection. Account quota remains UNKNOWN. Stop after CI for merge
authorization. No automatic recovery or activation is implied by merging.

Remaining: prove explicit product-domain stage mappings and checkpoint handoff,
trusted production caller, full resource/lifecycle admission, bounded real canary
and rollback, ten-day acceptance. 080-RUNNER remains CODE_IN_PROGRESS.
