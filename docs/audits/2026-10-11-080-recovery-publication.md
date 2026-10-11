# 080-RUNNER approved recovery candidate staging

Base: d6b7e861edae3d169a98798937e96c77622737bf (PR #174).
One opt-in client/server contract, no production runner registration.

RecoveryCandidatePublicationTransport requires RecoveryPipelineLease and
RecoverySharedBudgetLedger. It binds approved publish scope and exact context
before requests. The complete recoveryId/planSha256/scopeSha256 identity enters
the existing execution reference and durable intent protocol. Ordinary execution
and legacy uploads remain unchanged. Pending writes require explicit readback;
there is no blind second PUT, approval creation, scheduling or activation.

The server uses existing authentication and recovery flag. Recovery requires
ACTIVE registered approval, exact current lease owner/attempt/date and plan key
in one indexed conditional query. The existing bounded immutable plan reader
verifies canonical bytes/hash and deadline, target membership and publish scope.
Authority is checked again after the plan read and around R2 effects. The STAGED
release INSERT carries the joint lease/approval predicate in the same SQL
statement. No active pointer is changed. Existing upload_once marker behavior
remains; owner takeover cannot reset a prior unknown upload intent.

R2 and D1 are not one transaction. Revocation during an effect can leave immutable
orphans, but post-effect confirmation rejects acknowledgement. Plan bytes are
loaded once per request authority object, not once per artifact campaign; every
subsequent confirmation rechecks registered authority and immutable deadline.

Client request cost floors add two Class B plan reads and indexed confirmation
cost allowance. Floors are not a measured/full task upper bound: caller must
include shared ledger, guard, renewal, all artifact checks, bytes and platform
traffic. Physical D1 usage/account balance remains UNKNOWN; no production budget
admission or complete cross-host recovery acceptance is asserted.

Verification: full Python 939 passed; Web 212 passed, production build and lint
passed. Tests execute actual conditional SQL through local SQLite, including
revocation at INSERT; malformed identity, flag off, wrong scope/date/attempt,
expired approval, takeover before/after effect. Real recovery lease/shared budget
classes exercise client wire, unknown allowance and lost receipt/no repeated PUT.
Existing ordinary tests remain. No assertions lowered or tests skipped.
The Python SQL extractor explicitly materializes the ordinary template branch;
recovery SQL is executed by TypeScript SQLite tests. Local fixtures are not
production validation. Existing large chunk warning remains.

Manual code checklist review: parameterized PK queries, bounded exact plan reads,
existing credentials, stage_only v4, immutable content/intent, protected paths.
No migration, production callers, workflow changes, flags, collection or recovery
operations. Merge does trigger current main CI and automatic Worker deployment,
including pending migration check/apply. Remote pending status UNKNOWN; PR #174
deployment reported none. Stop after CI for merge/deployment authorization.

Remaining: product-domain mappings, trusted runner registration, full cost and
lifecycle admission, real bounded canary/rollback and ten-day acceptance.
080-RUNNER remains CODE_IN_PROGRESS.
