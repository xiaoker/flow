# 080-RUNNER: exact recovery scope in the existing shared account ledger

Base `dac896b2fc69f6065ba0be707c5c92a7aee15f6b`, PR #172 merged;
main CI `38067224021` passed. This is one end-to-end client/server budget
contract, not a production runner activation or a second budget database.

## Contract and compatibility

Ordinary requests keep their four identity fields, ordinary-only lease fence
and task IDs. Recovery requests add the complete triple `recoveryId`,
`planSha256`, `scopeSha256`; partial bindings are rejected. Recovery requires
the existing `PIPELINE_RECOVERY_ENABLED` flag, ACTIVE registered approval,
current owner/attempt/target/recovery lease and at least 30 seconds remaining.
The same predicate fences the initial SELECT and final account CAS. Revocation
or takeover between them cannot grant a reservation. No migration or new flag.

The server reads the exact registered plan through `readRecoveryPlanObject`:
one bounded HEAD/GET pair, maximum existing 300 KB, no listing. It verifies
object/semantic hashes, key, target date and immutable deadline. A renewal
cannot extend that deadline. The account row remains the existing single
`flow-account-v1` scope, with the same physical metadata/UNKNOWN behavior.

Candidate runner budgeting requires this explicit **inline** budget manifest
inside an approved plan (no existing plans are rewritten):

```json
{
  "runnerBudgetContract": "flow-shared-budget-v1",
  "taskCostUpperBound": {
    "d1RowsRead": {"value": 100, "unit": "rows", "basis": "estimated"},
    "d1RowsWritten": {"value": 10, "unit": "rows", "basis": "estimated"},
    "workerRequests": {"value": 10, "unit": "requests", "basis": "estimated"},
    "actionsMinutes": {"value": 1, "unit": "minutes", "basis": "estimated"},
    "r2Bytes": {"value": 100, "unit": "bytes", "basis": "estimated"},
    "r2ClassA": {"value": 1, "unit": "operations", "basis": "estimated"},
    "r2ClassB": {"value": 10, "unit": "operations", "basis": "estimated"}
  }
}
```

These numbers illustrate the shape only, not a real approved cost. Reuse the
existing seven-dimension metric schema; legacy source-specific manifests and
archived references without this proven inline upper bound fail closed for
the candidate ledger. Their old recovery path is not changed. A caller cannot
invent these fields after approval: planHash and exact bytes would no longer
match. No automatic plan regeneration/approval occurs.

Task IDs are `recovery/<id>/<planHash>/<runKey>/<owner>/<attempt>`.
Reservations for all attempts within that exact recovery/plan prefix count
towards the approved upper bound. Even known settlements conservatively keep
their original cost charged to that plan envelope; UNKNOWN holds are never
freed. A takeover must request only a remaining bounded cost, not reserve the
whole plan anew. This accounting is scoped to the existing retained account
state, not a claim about an externally reset ledger or the entire account.

`RecoverySharedBudgetLedger` reuses ordinary transport, operations, pending
reconciliation and replay rules. It locally checks exact context/stage scope,
upper bound and cost before network I/O. Server enforcement is still required;
a fake local verifier cannot confer authority. Missing recovery identity in
execution validation is rejected; ordinary-only artifact adapters stay closed.

## Resources, safety and acceptance

No account initialization, period rollover, reset, quota increase, new source
requests, approval, claim, active pointer, data date or production caller is
changed. Policy caps absent from config remain UNKNOWN; tests using injected
caps are not production quota proof. Official shared-account usage is UNKNOWN.

Each recovery budget request adds at most two R2 Class B plan reads and an
indexed approval lookup to existing bounded ledger operations. Client request
cap is min(32, plan maxRequests); existing lease guard requests also consume
their independent cap. Exhaustion stops before effects; this PR does not claim
those caps suffice for a complete live pipeline. Admission must include the
separately bounded control/renewal traffic, CI and deploy costs; no free
bootstrap calls or zero consumption are asserted.

Tests cover real Python -> localhost HTTP -> TypeScript -> SQLite admission,
ordinary/recovery shared-account contention, exact binding/hash/date/expiry,
revocation at CAS, takeover cost multiplication, replay and UNKNOWN rejection.
The full ordinary suite remains. SQLite fixture metadata is not D1 telemetry.

Merge includes `web/**` and thus triggers Worker deploy and the existing
pending D1 migration step. No new migration; current remote pending status
UNKNOWN. Await explicit merge + automatic deploy + pending migration check/apply
authorization. No recovery flag may be enabled or task run by this PR.

Next: recovery-aware checkpoint/publication authority and domain-specific stage
mapping, then trusted caller and budget/lifecycle/canary validation. 080 remains
CODE_IN_PROGRESS, not production recovery complete.
