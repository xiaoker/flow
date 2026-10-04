# 080-RUNNER candidate upload authority

Base: merged PR #161, `d631222cfa3162a8d32b6e3601d18028fd7f26dd`; main CI `37139268189` succeeded. Isolated branch `codex/080-candidate-authority-20261004`.

## Scope

The existing read-model upload endpoint accepts an optional explicit `pipelineExecution` with runKey, leaseOwner, attemptCount, targetDate and producerBuildSha. Presence with malformed data fails closed. It is restricted to v4 `stage_only`, matching manifest asOf/build identity. Existing callers omitting the field keep the previous behavior and authentication; no new credentials, endpoint, table, feature flag or migration.

Reuse the existing indexed pipeline-window lookup. Only current, unexpired ordinary DISPATCHED/RUNNING leases with a 30-second safety margin authorize effects. Recovery tasks, completed tasks, wrong owner/attempt/date and unavailable D1 are rejected. The reference does not grant shared-account resource admission or prove producer provenance.

Runner R2 writes use conditional creation and exact hash/size readback. Checks before/after effects detect lease loss; R2 and D1 are not one transaction, so an immutable orphan object can remain after takeover. An orphan is not success, completion or activation. Final D1 staging uses an INSERT SELECT with the lease predicate in the same statement, `ON CONFLICT DO NOTHING`, and exact STAGED readback. Existing ACTIVE or conflicting candidate rows are never overwritten. No active pointer is read or written by this explicit path.

Decoded candidate content is bounded to 1.5 MB/512 chunks; immutable readback checks expected size before buffering. The existing manifest contract limits artifact count. Normal runner non-final upload performs three indexed lease checks, one conditional R2 put and one exact R2 get. Finalize performs eight indexed lease checks, one exact release lookup, one conditional release insert, two conditional puts, two exact gets and bounded artifact heads. Physical D1 row telemetry/account budget remains UNKNOWN; operation counts are not provider billing proof. No bucket listing, retries or lease mutation is introduced.

This is authority protection, not the completed runner publication adapter, artifact-content QA or production acceptance. Future transport must still preserve/verify producer canonical manifest bytes, reconcile ambiguous uploads, verify the exact candidate through existing read APIs, and account shared budget. Ordinary/recovery executor integration and separately authorized canary remain pending. 080 stays CODE_IN_PROGRESS.

## Verification

- Full Python: 791 passed, including nine tests executing the actual conditional SQL against SQLite and its indexed query plan.
- Web build/tests/lint: final results reported in the PR (190 tests). Built endpoint tests cover auth, malformed reference, current lease, final STAGED response, completed rejection, safe provider errors and legacy upload compatibility. Fixtures are local evidence only.
- Additional `npx tsc --noEmit --incremental false`: both current branch and the exact base tree report 136 existing diagnostics; normalized comparison shows zero added diagnostics. This is not a passing repository typecheck. No assertions were lowered.
- Review checklist sections 1, 3–6 applied: no formula/classification changes, no date substitution, no new authentication, indexed/bounded writes, no live verification claim. Existing client chunk-size warning unchanged.

## Merge effects / rollback

Changes under `web/**` match Deploy Flow Worker. Merge automatically runs Worker deployment and **all pending D1 migrations**, even though this PR adds none. Current remote pending migration state is UNKNOWN and was not queried or changed. Do not equate no added migration with no deployment effects. Review the companion merge-side-effects JSON before authorizing merge/deploy.

No production upload, collection, workflow dispatch, flag/pointer/retention changes or remote writes during implementation. Original protected reports, memory and user files untouched. Revert this code restores legacy handling; any future candidate data requires normal evidence-based reconciliation, not automatic deletion.

Stop after PR CI; wait for explicit merge/deployment authorization. No new execution gate or approval platform is introduced.
