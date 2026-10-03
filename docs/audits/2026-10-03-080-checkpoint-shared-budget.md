# 080 checkpoint shared-budget identity fence

Base: `4c322fe29a68e02e1ec119156fde91d73df6db69` (PR #155).

Candidate checkpoint transport already accepts SharedBudgetLedger through its
BudgetLedger contract. Before a shared authorization it now checks that runKey,
real targetDate, owner, attempt and task ID equal the object's execution identity.
A grant for another still-valid task is not permission for this object's I/O.
This equality check is local and grants no authority; existing server lease
fencing and atomic budget transitions remain required and unchanged.

Evidence distinguishes configured server-authoritative shared accounting from
the in-process reference. A shared reservation claim is set only after a confirmed
non-replayed authorization. Observed budget control-plane calls and D1 meta are
reported separately from object endpoint consumption. Missing meta stays UNKNOWN;
account-wide remaining quota is not inferred. Reading evidence performs no GET.

Nine composition tests cover the identity fields, confirmed grant, refusal,
over-budget/replay, ambiguity and no hidden accounting requests. Their scripted
HTTP receipts are fixtures, not production D1 or cross-host acceptance. Existing
real localhost Python-to-Worker/SQLite tests also remain green; these likewise
are not official account telemetry.

Verification: full Python 692 passed; focused suites 46 passed. Web 177 tests,
build and lint passed. Independent read-only review found no actionable defect.

No new CLI/workflow or production entry, no migration, evidence provisioning,
period reset, lifecycle installation, collection or pointer mutation. Original
reports/memory remain untouched. Merge causes CI only, no Worker auto-deployment.
080 remains incomplete: provider lifecycle evidence, shared-account reconciliation,
full-task resource proofs, remaining adapters/recovery and authorized acceptance.
