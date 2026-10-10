# 080-RUNNER: existing recovery approval and cooperative lease

Base: `7494d9ce26cba61b4d6426f632cf99bac90f48a2` (PR #171).

This candidate client does not claim, register, approve, complete, publish,
activate, alter flags or grant budget. No production caller is registered.
`RecoveryPipelineLease` reuses the ordinary lease lifecycle, lock, time and
request caps through one context-validation hook; ordinary guards still reject
recovery. The recovery transport reuses `BoundedHTTPExchange` (20-second caller
deadline, 64 KB / 64 chunks, no redirects/retries). An unconfirmed exchange never
changes guard state; failures are sticky and ambiguous renewals remain visible.

Before the first request the immutable approved plan is independently copied,
validated and bound to exact context and identical stage semantics. Only
canonical `plan_bytes` registration is supported: semantic planHash and exact
scope byte hash are distinct and both must match the existing approval record.
The guard does not download a plan, invent domain-to-stage mappings or trust a
caller-supplied verified Boolean as live approval.

Each fresh proof reads the existing recovery endpoint plus the current ordinary
status endpoint. The former lacks target/window/recovery row identity fields;
the latter proves current recoveryId, owner, attempt, windowDate and targetDate.
Both must match. ACTIVE approval, plan object key/hash, approval expiry, plan
deadline and both lease expiries are checked. The earliest expiry wins. Separate
GETs are not an atomic transaction: all eventual server-side effects must retain
their own owner/attempt/recovery fences; this client is not a replacement.

Renewal uses only the existing recovery/status RUNNING + renewLease endpoint.
It reserves space for three control requests before writing. A compact valid
renewal receipt records measured rowsWritten but never substitutes for renewed
approval: both GET proofs must pass afterwards. Expired leases are not revived;
revocation, terminal state, HTTP errors, wrong dates/owner/attempt stop effects.
Concurrent cooperative checks share one renewal. No background heartbeat is
started, so long computations must hit a boundary before their next effect.

Bounds: at most min(32, approved maxRequests) control requests, 3 per renewal,
2 per fresh confirmation, and at most 3600 execution seconds (also constrained
by approval/deadline). This is a control request cap, not a reservation for all
source/storage work. GET rowsRead and account remaining quota remain UNKNOWN.
No quota safety, production approval or complete recovery result is claimed.

Verification: complete Python and Web tests/build/lint are required; fixtures
use local injected sessions, not production credentials. Existing real local
HTTP ordinary tests and bounded HTTP tests remain in the full suite. Real
production approval/Access/migration verification is not performed by this PR.

Next: recovery-aware server/client budget and checkpoint/publication authority,
then domain-specific stage mapping and a trusted caller/channel. Ordinary-only
restrictions on those transports remain unchanged. 080 is CODE_IN_PROGRESS.
