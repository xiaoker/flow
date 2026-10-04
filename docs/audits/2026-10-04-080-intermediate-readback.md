# 080 ordinary intermediate upload reconciliation

Base: PR #166 merge `e0f7428b0a28ac4f1f020495e9615baf0225584f`.
Branch: `codex/080-intermediate-readback-20261004`.

## Cause and contract

The STAGED-only read API cannot see an intermediate R2 object before the release
row exists. A lost successful intermediate POST therefore left the retained
intent unknown and could not resume safely. This change uses the existing
read-model-import endpoint with explicit `operation: inspect_artifact`.

Inspection requires existing ingest authentication, ordinary schema-v4 execution,
stage_only, finalize=false, exact producer manifest bytes/self-hash, and the same
artifact descriptor/content validation as upload. It confirms owner, attempt,
real date, nonrecovery status and lease before/after the exact R2 get and again
after reading. It never calls put, head, release insertion or pointer mutation.
The exact key is taken from the validated manifest, never a list or prefix scan.

The object size/metadata and streamed actual bytes are checked; read limits are
1.5 MB and 512 chunks. Wrong metadata/content is 409; missing is 404. Neither
response proves an earlier effect did not happen or permits another upload.
Success attests one artifact only, not a STAGED/complete release.

The transport retains the same local PENDING intent until exact receipt and
current lease are confirmed, then records the normal staged-artifact receipt.
Inspection is counted as a bounded request/bytes, not another upload attempt.
Shared allowance must cover at least four D1 lease reads, one R2 read and no R2/D1
write; these floors are estimates, not measured provider/account quota.

The existing runner adapter reconciles any pending artifact before replaying
acknowledged receipts, then uploads only missing artifacts. Final unknown writes
still use the STAGED readback. All-artifact STAGED verification remains required
before stage completion. Existing legacy upload behavior is preserved.

## Verification

- Python full suite: 848 passed; publish-adapter suite: 15 passed.
- Web: 192 passed, production build and lint passed.
- Exact Worker fixture proves zero R2/D1 writes, no release registration,
  authentication/invalid-finalization rejection, missing/corrupt object rejection
  and owner takeover rejection.
- Restart fixture reopens retained SQLite state; seven unique artifacts have
  seven uploads total, not eight. Missing readback preserves unknown outcome.
- `git diff --check` passed. Loopback fixtures are local, not production proof.

Commands: `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`;
Web `npm test` (build included), `npm run lint`.

## Effects and next step

No production requests, collection, flags, pointer changes, cleanup or protected
original-file changes during implementation. PR/main CI consumes Actions minutes.
This PR changes web paths: merge automatically triggers Worker deploy and the
existing remote migration-check/apply flow. No new migration files. Current
production pending migration state is UNKNOWN, not asserted empty.

Stop after hosted CI for explicit merge/deploy-side-effect authorization.
080 remains CODE_IN_PROGRESS: cross-environment retained intent/checkpoint
integration, recovery wiring, exact deployment/live acceptance and separately
authorized canary/rollback package remain. No production runner registration.
