# 080-RUNNER: recovery control request envelope

Base: `504e95750e42d0dfb603794d0e97775c7a83f783` (PR #178).
Deployment 38116212696 succeeded; no pending migrations; exact build verified.

## Purpose

Extend the existing offline task preflight, not a second quota service. Recovery
control requests must include the nested shared ledger and fresh lease proofs.
Each checkpoint operation uses two ledger requests, four ledger confirmations
and two transport confirmations. Each recovery confirmation reads approval and
current lease. Thus two object operations consume 26 lease requests including
initial verification; three consume 38 and exceed the existing 32-request cap.

The pure envelope checks declared checkpoint operations (including readbacks),
standalone ledger transitions, runner confirmations and renewal upper bounds.
Missing counts remain UNKNOWN and block; bool/negative/oversized counts reject.
The effective cap is the lower of local guard limit and approved plan limit.
Existing task preflight consumes these blockers and compares the combined
lease/checkpoint worker estimate to the declared control request bound.

No request cap is raised, no authority check is removed, no retry is hidden.
The envelope does not reserve resources or authorize execution. Physical D1
rows and account capacity remain UNKNOWN, not zero. Counts are caller-supplied
estimates, not automatic proof that every execution branch is included.

## Local validation

- Full Python: 970 passed, 20.73s.
- Web `npm test`: production build and 215 tests passed; `npm run lint` passed.
- Targeted control/task preflight: 29 passed. Existing guarded transport fixture
  measured 26 lease calls plus four ledger and two object calls, matching the
  declared two-operation envelope. This is local fixture evidence only.
- Existing large-chunk build warning remains. No production verification is
  claimed for this offline Python change.

## Boundaries and next item

No production caller, workflow, migration, flag, pointer or remote operation is
changed. The existing CLI is not given a new command. One Python implementation
file plus tests and audit/progress documentation only.

This makes the known cap mismatch reproducible; it does not make a complete
recovery runnable. Full runner envelope generation, budget lifecycle transitions,
all nested publication costs, lease heartbeat/time bounds and trusted entry
integration remain before any real canary. In particular, using a fresh guard
without accounting for aggregate task cost is not a quota workaround.

080-RUNNER remains CODE_IN_PROGRESS. Account capacity UNKNOWN. No production
recovery/collection or canary was run. Original protected files untouched.
