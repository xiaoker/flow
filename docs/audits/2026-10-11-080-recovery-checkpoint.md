# 080-RUNNER: approved recovery checkpoint authority

Base `5f4da7e72e760b27ff8cbd773791a71ff9e6fe40` (PR #173). Main CI
`38103045143` and deployment `38103045444` passed. That deployment reported no
pending migrations and verified the exact served build on its first attempt.
Worker version: `de48c1cd-8b44-4cda-875a-600bec160c54`.

## Contract

This is one opt-in client/server checkpoint contract, not a live recovery
entrypoint. Ordinary requests retain their endpoint, namespace, authentication,
limits and ordinary-only lease check. No scheduler or production caller changes.

Recovery adds the complete `recoveryId`, `planSha256`, `scopeSha256` triple.
Partial identity rejects before storage. The existing recovery flag must be
enabled by a separately authorized operation. A primary-session, bounded SQL
query checks exact run/owner/attempt/target/recovery identity and, in the same
statement, EXISTS an ACTIVE registered approval with matching hashes/key.
Both lease and approval must retain more than 30 seconds. The query uses the
existing primary keys and LIMIT 1; no schema or D1 writes.

The existing immutable plan reader verifies the canonical bytes and semantic
hash through exact-key HEAD/GET. Target must be in dataDates and stage must be
approved. The immutable plan deadline is checked again at every subsequent
confirmation; renewal cannot extend it. Only identical `core_collection` and
`publish` stage semantics are supported. Generic build/normalize/verify and
handoff are deliberately not inferred from dashboard or other product-domain
approval. Their mappings remain a separate unfinished requirement.

The namespace is:

```
market-data/pipeline/checkpoints/v1/recovery/<recoveryId>/<scopeSha256>/<runHash>/<producer>/<stage>/<input>/<objectHash>.json
```

Owner/attempt are authority, not object identity. Takeover can reuse an exact
same-plan immutable object but cannot mix approved plans or ordinary namespaces.
Metadata binds the recovery triple as well as ordinary reference fields. The
existing digest, byte/chunk limits, conditional create and exact readback remain.

R2 writes and D1 authority are not a cross-service transaction. Authority is
confirmed immediately before a PUT and after readback. An intervening takeover
may leave an immutable orphan; it never grants completion or activation, and
cannot be acknowledged after authority is lost. No cleanup or deletion occurs.

## Client and resource boundary

`RecoveryCheckpointObjectTransport` requires the existing recovery lease guard
and `RecoverySharedBudgetLedger`; it cannot fall back to an in-process quota.
Before HTTP it validates exact plan/context/stage and the ledger execution
identity. All traffic reuses the bounded exchange and pending-write/readback
protocol. A lost receipt permits one independently budgeted exact GET, never a
blind second PUT; UNKNOWN allowance blocks that GET without clearing the hold.
The client request limit is min(existing 12, approved plan maxRequests).

Each successful recovery checkpoint request adds one plan HEAD/GET pair:
3 Class B operations including object read; POST adds at most one Class A
conditional PUT. Up to four joint confirmations consult the two indexed rows.
Client floors include these operations but **are not a complete cost upper
bound**: the caller must include ledger HTTP/SQL/CAS, lease confirmations and
renewals, bytes, execution/CI/deploy and retry traffic in all seven dimensions.
Independent request caps do not prove a complete task fits its approved total.

D1 physical rows_read remain UNKNOWN in this interface; rows_written=0 describes
its read-only SQL, not the shared ledger's separate CAS operations. Bytes read
are not a claim of retained storage growth. Account availability and installed
retention policy remain UNKNOWN. No live budget admission is claimed.

## Verification and remaining work

Python client tests use real recovery guard/budget classes with explicit local
HTTP-response fixtures, not real account telemetry. TypeScript tests execute
the actual SQL against local SQLite and verify indexed plans, exact approvals,
flag/auth rejection, wrong owner/attempt/date/hashes, deadline, plan corruption,
revocation between plan read and PUT, and orphan writes after takeover. Ordinary
transport tests continue unchanged. Local mocks are not production acceptance.

Full local Python: 936 passed. Web: 207 passed, production build and lint passed.
Existing >500 kB chunk warning remains; no assertion lowered or test skipped.
Hosted CI result belongs in the PR report after pushing the final code.

Manual review used `docs/code-review-checklist.md`: parameterized/indexed SQL,
bounded exact keys, existing auth/flag, immutable provenance and no active
pointer/lease mutation. No production collection, approval, claim, source access,
canary, flag change, migration or protected original-worktree edits occurred.

Merge triggers existing main CI and Worker deploy, including the existing
pending-migration apply step. No new migration; current remote pending status
UNKNOWN (last deploy reported none). Stop after CI for combined merge/automatic
deploy/pending migration check authorization. On failure do not activate recovery;
revert code through a normal reviewed PR if needed. Ordinary namespaces remain.

Next: recovery-aware candidate publication authority and exact product-domain
mapping, then trusted caller, full cost/lifecycle and bounded real canary/rollback.
080-RUNNER remains CODE_IN_PROGRESS; this is not restored data or a production
switch.
