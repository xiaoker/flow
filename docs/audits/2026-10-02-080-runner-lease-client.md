# 080-RUNNER: live ordinary lease guard (candidate only)

Base main: `d8cb795b6f2e083874af092bd3a112d80bbf59aa`.
PR #144 deployed via run [36993056006](https://github.com/xiaoker/flow/actions/runs/36993056006).
The workflow checked out this exact merge SHA, found no pending D1 migration,
deployed Worker version `5cf99640-bd20-441b-9f34-30afd12c9656`, and verified the
served build SHA on attempt 2. This is code deployment evidence, not data/canary
acceptance or proof of current flags/account-wide budget.

## Scope

`OrdinaryPipelineLease` is an explicitly injected ordinary-run guard for the
candidate Python runner. It uses the existing INGEST bearer and Access service
token and the existing pipeline-window status endpoint. It cannot claim, bind
a date, complete a window, dispatch, or mutate an active release. Recovery
contexts are rejected; the approved recovery guard/receipt path remains required
and unchanged. The CLI and scheduled/manual production workflow paths are not
wired to this client.

Initial GET proves runKey/windowDate/targetDate/owner/attempt/live status and
expiry. Boundary checks renew cooperatively when due, with serialized concurrent
checks. Default interval is 180 seconds (allowed 60–300), ceiling 32 total HTTP
attempts and 3,600 seconds. No background process, hidden retry or redirect is
used. Response size is capped at 64,000 bytes, request connect/read timeouts are
5/10 seconds, and streamed reads have a checked deadline. Credentials can only
be sent to the configured Flow HTTPS origin; explicitly enabled loopback is
solely for local fixtures.

The core adapter checks the guard after pacing and before every actual source
request/redirect hop. In-flight HTTP requests are not forcibly cancelled, and
long CPU-only work does not renew in the background; expiration blocks its next
side effect or result. Before checkpoint/result persistence, a fresh GET or due
renewal proves authority again. Lease failures stay sticky and propagate through
the existing collector rather than becoming soft data warnings. Checkpoint
output reuse, not the lease guard, remains responsible for artifact validation.

## Evidence and resource semantics

The receipt records actual control-plane request and renewal counts and valid
`rowsWritten` metadata. Physical write rows are not confused with logical
single-row changes. A lost/invalid renewal response retains an unconfirmed write
outcome; read metadata and account-wide remaining quota are UNKNOWN, not zero.
The request ceilings are safety bounds, not proof of available free-tier budget.
No live run is permitted before the runner's remaining durable checkpoint,
stage adapter, budget reservation, recovery and canary prerequisites are met.

## Verification

- Full Python regression: 541 passed. Web production build/tests: 159 passed;
  Web lint and `git diff --check` passed.
- Deterministic tests cover owner/attempt/date/recovery/terminal/expiry rejection,
  20 concurrent checks sharing one renewal, no per-boundary heartbeat polling,
  bounded requests/time/bytes, redirects/auth errors, secret-safe timeouts,
  ambiguous write outcomes and blocked source/completion effects.
- Real localhost HTTP integration exercises requests transport and exact route,
  headers and payload. It uses synthetic fixtures, not production validation.
- Existing fixture CLI/command integration remains part of the complete suite.
- Production operations, official source requests and flag changes: none.

## Merge side effects and next handoff

Only src/tests/docs change. The current `deploy-worker.yml` matches `web/**` or
itself, so this PR does not trigger Worker deployment or remote migrations.
Pull-request CI and main-push CI consume Actions minutes (job timeouts 10 and
15 minutes plus branch freshness). Account-wide remaining budget is UNKNOWN;
one necessary CI run is within the existing code/CI authorization, not permission
to increase spending. No new deployment, collection or write-path activation is
requested. Rollback is to leave the candidate client uninjected; existing
production entry points have not changed.

After CI, stop for merge authorization. After merge, continue durable cross-job
checkpoint transport and remaining shared stage adapters, then prepare a bounded
single-window canary package; do not begin canary or production cutover here.
Protected original reports/phase0/latest-*, memory/ and user files are untouched.
