# 080-RUNNER: ordinary lease renewal control plane

Base: `75dce07761cb79fc1692b4be1bd2b1057806e58d` (PR #143 merged).

This increment adds an opt-in `renewLease: true` operation to the existing
authenticated pipeline-window status endpoint. It requires `status: RUNNING`
and a positive `expectedAttemptCount`. The operation preserves stored status,
target date, source fingerprint and attempt count; it is not a status transition.

The primary-key guarded UPDATE requires the current owner and attempt, an
unexpired non-null lease, DISPATCHED/RUNNING status and no recovery ID. It extends
the lease to at least server time plus 15 minutes, never shortening it. Caller
supplied duration is not supported. Expired, terminal and recovery leases cannot
be renewed. D1 mutation metadata is validated and rowsWritten is returned;
this is not an account-wide resource usage report.

No workflow, client heartbeat, migration, flag or production data changes are
included. Existing clients that omit renewLease retain their behavior. The next
increment must wire an externally verified lease and bounded heartbeat into the
runner; this endpoint alone does not make the runner production-ready.

## Verification

- Full Python suite: 511 passed.
- Web build and suite: 159 passed; lint passed.
- SQLite fixtures cover owner/attempt/expiry fencing, terminal and recovery
  isolation, no lease shortening, date/status preservation and malformed metadata.
- Built Worker tests cover existing authentication and malformed renewal rejection.
- No production API calls, collection, D1/R2 writes or deployment performed.

## Merge side effects

Changes under web trigger the repository's automatic Worker deployment workflow
on merge to main. That workflow may apply existing pending D1 migrations before
deployment, although this PR adds none. Pending production migration state has
not been checked in this code-only phase. Merge/deployment requires the next
explicit approval; no live canary is authorized by this PR.

Original checkout and protected reports/phase0/latest-* and memory/ were not edited.
