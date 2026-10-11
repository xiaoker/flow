# 080-RUNNER: bounded dashboard recovery stage mapping

Base: `2e2055b73acdd541db74c338851491969c64c026` (PR #176).

## Contract

- Approved `dashboard` maps only to core `build` and `verify`. Collection/publication still require explicit approval.
- Runner requires exact `CoreReadModelBuildAdapter` / `CoreReadModelVerifyAdapter` types (no subclasses/replacements) and the existing `RecoveryPipelineLease`, before authority callbacks or state writes.
- Existing builder outputs only dashboard, with materialized source, exact snapshot/sidecar hash/date, coverage and isolated workspace limits. Verify recomputes the saved build and rejects pending/blocked publication. No PCF, stock-flow or penetration permission is inferred.
- Existing checkpoint client/server allow build/verify only with immutable dashboard approval, retaining flag/auth, exact approval/lease fencing, hash, namespace, shared budget, operation limits and conditional writes.
- Pure mapping evidence remains `liveAuthorityVerified=false`.

## Validation

- Full Python: 955 passed, 18.89s (`PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`).
- Web: `npm test` build and 213 tests passed; `npm run lint` passed. Existing large-chunk build warning remains.
- Actual core adapters run through fixture-authorized runner; resume uses saved inputs after original deletion. Arbitrary adapters/subclasses reject before I/O; pending/blocked still reject. Recovery checkpoint transport uses same lease/budget; server tests use real SQLite SQL and bounded object fixtures, including unrelated domain rejection.
- Local fixtures are not production canary. Account usage and pending remote migrations UNKNOWN. No new migration, flags, production caller, active pointer, collection, registration or canary.

## Boundary and next step

Web changes trigger Worker deployment and pending migration apply after merge: await combined authorization. No deployment is dispatched by this code task.

080-RUNNER remains CODE_IN_PROGRESS: cross-host handoff/discovery, trusted formal entry, full-task cost/lifecycle, independently authorized real canary/rollback and ten-day acceptance remain. Ordinary behavior and original protected files unchanged.
