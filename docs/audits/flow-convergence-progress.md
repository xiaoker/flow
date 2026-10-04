# Flow convergence progress

This is a human-readable handoff index for the roadmap at
`docs/handoff/2026-09-23-flow-target-architecture-and-convergence-roadmap.md`.
It is not a production gate, database protocol, or replacement for GitHub,
R2, D1, release, or receipt evidence.

## Current stage

### 2026-10-04 injected candidate publish adapter

- PR #165 merged `6eee4309b790d8807fc6fe8defbe9034a338e798`; main CI `37198596074` passed, no Worker deployment or migration.
- Fresh branch `codex/080-publish-adapter-20261004` connects verified immutable core output to ordinary runner publish through explicit injection. Shared allowance, retained intent and current lease required; all declared artifacts exact-readback verified.
- Twelve new behavioral fixtures include actual runner invocation, no duplicate uploads on resume and unknown-final reconciliation ordering. No CLI/workflow production registration or live acceptance.
- Audit/effects: `2026-10-04-080-publish-adapter*`. Protected user files and production state untouched; stop after CI for merge authorization.
- Remaining: intermediate unknown-write reconciliation, cross-host intent/checkpoint integration, recovery and live canary/rollback. 080 is CODE_IN_PROGRESS.

### 2026-10-04 local durable publication intent

- PR #164 merged as `5a78ab0f7c4a736b496c10a2fae27eb4e9975515`; main CI `37174851707` passed, no Worker deploy/migration triggered.
- Fresh branch `codex/080-publication-intent-20261004` from that main adds explicitly injected SQLite pre-HTTP intent and persisted exact receipt. Reuses existing transport/lease/budget/readback; no production registration.
- Restart and separate processes sharing the retained file cannot forget unknown writes. Twenty concurrent instances yield one intent winner; intermediate unknown writes remain blocked pending later reconciliation.
- Audit/effects: `2026-10-04-080-publication-intent*`. Local evidence does not prove remote visibility or account quota. No protected original changes or production actions.
- Stop after CI for merge authorization. Runner integration, cross-host persistence, intermediate uncertainty, recovery and live acceptance remain; 080 is CODE_IN_PROGRESS.

### 2026-10-04 bounded injected candidate HTTP transport

- PR #163 merged `5506c1da139f623d16265d7a563894a7a7b26525`; main CI `37174011540`, deploy `37174011632` passed. Exact merge build verified on attempt 3; no migrations to apply. Worker `d86bdab3-42e9-4366-bab6-dcc50b518b6f`.
- Fresh worktree `/private/tmp/flow-080-candidate-http-20261004`, branch `codex/080-candidate-http-20261004`, based on that merge.
- Injected ordinary-only stage publication transport uses the existing wire, lease, budget and bounded HTTP contracts. Exact final readback; unknown intermediate writes block blind retries. No runner/CLI/workflow registration.
- Python 825 / Web 191 tests, build/lint passed. Audit/effects: `2026-10-04-080-candidate-http*`. No provider quota or live Access acceptance claimed.
- Python/tests/docs only: PR/main CI, no automatic Worker deployment or migration. No collection, upload, flags, pointer, retention or protected original-file edits. Stop after hosted CI for merge authorization.
- Next: persistent pre-effect publication intent/checkpoint and intermediate uncertainty reconciliation in existing runner, then recovery/resource/live acceptance. Instance-local transport hold alone is not durable resume. 080 remains CODE_IN_PROGRESS.

### 2026-10-04 exact producer manifest bytes across candidate upload

- PR #162 merged `860d7673f8e8dd85649ff078bc85864e24f4c75b`; main CI `37172358522` and deploy `37172358765` passed. Exact production SHA verified; no migration pending then.
- Fresh branch `codex/080-publication-transport-20261004` fixes the reproduced Python decimal/JS reserialization hash seam through an optional bounded signed producer JSON field and pure shared candidate upload helper. No new serializer or caller registration, and no actual production data defect claimed.
- Web build/191 tests/lint passed; full Python results in PR. Audit/effects: `2026-10-04-080-candidate-manifest-bytes*`. Local byte fixtures are not production acceptance.
- Merge matches Worker deploy/pending-migration workflow; no new migration, current remote pending state UNKNOWN. No live upload, collection, flag, pointer, retention, remote migration or protected-file changes.
- Stop after hosted CI for merge/deployment authorization. Bounded budget-aware HTTP transport, runner publication/recovery integration and live acceptance remain; 080 is CODE_IN_PROGRESS.

### 2026-10-04 explicit runner candidate upload authority

- PR #161 merged `d631222cfa3162a8d32b6e3601d18028fd7f26dd`; main CI `37139268189` passed. No Worker deployment/migration for that Python-only increment.
- Fresh isolated branch `codex/080-candidate-authority-20261004` protects explicit ordinary runner uploads with current lease/owner/attempt/date checks, conditional immutable R2 writes and atomic lease-conditioned STAGED insertion. Legacy uploads remain unchanged; no runner caller enabled.
- Audit/effects: `2026-10-04-080-candidate-upload-authority*`. Full Python 791 passed; Web final results in PR. Supplemental typecheck has the same 136 baseline errors, zero added. Fixtures do not establish production acceptance or resource allowance.
- This PR changes web runtime: merge automatically deploys Worker and applies pending migrations (remote pending state UNKNOWN); no new migration. Stop after CI for merge/deployment authorization. No live upload, collection, flags, pointers, retention or protected-file changes.
- Remaining bounded/budget-aware publication transport, recovery integration and live acceptance are still required. 080 remains CODE_IN_PROGRESS.

### 2026-10-04 shared publication contract extraction

- PR #160 merged `5f793b7bf04014bb4637fa867140658074346bb3`; main CI `37138580284` passed. No Worker deploy/migration.
- Fresh branch `codex/080-publication-contract-20261004` moves existing upload request construction/capability validation into a reusable package module; the current publisher delegates without transport or activation changes. Not a new publication platform or runner registration.
- Python 782 / Web 177 tests, build/lint passed. Audit/effects: `2026-10-04-080-publication-contract*`; hosted results reported with the PR. Local fixtures do not establish production acceptance.
- No candidate upload, collection, flags, pointer, retention, migration, production read change or protected user-file edits. Stop after CI for merge approval; next is the remaining candidate publication seam with bounded budget/lease-aware transport. 080 remains CODE_IN_PROGRESS.

### 2026-10-04 offline whole-task resource preflight

- PR #159 merged `bd2749a76a3ead70f4242c74f6b4a30f3e65eef7`; main CI `37137817176` passed. No Worker deploy/migration.
- Fresh branch `codex/080-task-preflight-20261004` aggregates bounded plan stage costs and explicit lease/control, checkpoint and platform overhead; existing bundle, budget and lifecycle contracts remain authoritative.
- Missing cost/usage/evidence is UNKNOWN and blocked; local consistency never grants execution authority or reserves resources. Audit/effects: `2026-10-04-080-task-preflight*`. Local and hosted test results recorded in the PR.
- No production entry/reads, workflow, migration, flag, retention, pointer, live collection or protected user-file changes. Stop after CI for merge approval. Remaining adapters/runtime evidence and separately authorized live acceptance remain pending; 080 is CODE_IN_PROGRESS.

### 2026-10-04 offline core verification increment

- PR #158 merged `aa275167ed2b2ed5ee7461c374be6cc1523eab41`; main CI `37123306214` passed. No Worker deployment/migration.
- Fresh branch `codex/080-verify-20261003` implements the existing runner verify seam through the same core builder checkpoint verifier, with saved bytes, confirmed-publication requirement and cross-job resume. Core verification does not require PCF shards.
- Python 740 / Web 177 tests, production build and lint passed. Hosted CI awaits standalone PR. Audit/effects: `2026-10-04-080-core-verification*`.
- No remote writes, CLI/workflow registration, flags, pointer or protected-file changes. Stop at PR CI for merge approval; afterward continue remaining adapters/resource evidence. 080 remains CODE_IN_PROGRESS, not production-ready.

### 2026-10-03 immutable historical build inputs increment

- PR #157 merged `30c167f1038e4bdaee1c79b45e59cc35893c478a`; main CI `37119570876` passed. No Worker deployment or migration.
- Fresh branch `codex/080-build-inputs-20261003` extends the same candidate build adapter with hash-bound market history/previous manifest bytes. Existing builder date/coverage regression checks remain authoritative; saved sidecars participate in checkpoint verification and cross-job resume.
- 28 focused tests passed; full local Python 720 / Web 177 tests, build/lint passed. Hosted CI awaits the standalone PR; no production acceptance is claimed. Audit/effects: `2026-10-03-080-core-build-inputs*`.
- CLI/workflows/production flags unchanged; no historical collection or pointer change. Stop at PR CI for merge authorization, then continue remaining normalization/verification/publication/recovery adapters from latest main. Full-task resource and live canary prerequisites remain; 080 is CODE_IN_PROGRESS.

### 2026-10-03 offline core build adapter increment

- PR #156 merged `42c7b3459d2416243911631f4aa47725b8a41b9f`; main CI `37105591284` passed. No Worker deployment or migration.
- Fresh branch `codex/080-build-adapter-20261003` implements a real offline build adapter through the existing runner injection interface; exact snapshot/date/hash validation and deterministic existing-builder outputs, with checkpoint verification and separate-job bundle resume.
- Full Python 706 / Web 177 tests, build/lint passed; hosted CI awaits the standalone PR. Audit and effects: `2026-10-03-080-core-build-adapter*`.
- No CLI/workflow registration, production path, PCF requirement, formula, flag or live execution change. Optional historical sidecar inputs and remaining adapters are not yet wired; no production/period acceptance is claimed.
- Stop after PR CI for merge authorization; after merge/main CI continue remaining adapters and immutable sidecars. Full-task budget/lifecycle/recovery/canary evidence remains required. 080 is CODE_IN_PROGRESS.

### 2026-10-03 checkpoint/shared-budget composition increment

- PR #155 merged `4c322fe29a68e02e1ec119156fde91d73df6db69`; main CI `37104059663` passed. No Worker deployment or migration.
- Fresh branch `codex/080-shared-checkpoint-20261003` binds shared budget identity to checkpoint I/O and reports observed control-plane evidence separately; no new production entry.
- Python 692 / Web 177 tests, build/lint passed. Details and effects: `2026-10-03-080-checkpoint-shared-budget*`. Composition fixtures are not production acceptance or official usage.
- Stop after PR CI for merge authorization. Provider lifecycle evidence, evidence reconciliation, full-task bounds, remaining adapters/recovery and separately authorized canary remain. 080 is CODE_IN_PROGRESS.

### 2026-10-03 lifecycle evidence consistency increment

- PR #154 merged `e73633c8f74e22afb68d319e74c30c43d3b18830`; main CI `37103089242` passed. No Worker deploy or migration.
- Fresh isolated branch `codex/080-checkpoint-policy-20261003` adds pure local consistency checks for exact-prefix expiration, locks, fresh complete reference audit and restore evidence; sizing preflight blocks missing lifecycle input.
- Passing caller assertions are not provider verification or runtime authorization. No retention policy is installed. Details and merge effects: `2026-10-03-080-checkpoint-lifecycle-inputs*`.
- Stop after PR CI for merge authorization. Actual lifecycle evidence, shared-account rollover, full-task budgets and remaining adapters remain; 080 is CODE_IN_PROGRESS.

### 2026-10-03 bounded control I/O increment

- PR #153 merged as `7532a2538e50f5c0938deac2f84651923abf1e83`; main CI `37102275281` and deployment `37102276562` passed. Worker identity verified; migration 0015 applied, no pending migrations afterward. No budget provisioning or production task executed.
- Candidate HTTP exchange is shared by budget and checkpoint clients; foreground deadlines do not claim cancellation. Unconfirmed uploads require exact verified readback before another upload.
- Evidence: `2026-10-03-080-bounded-control-io.md` and its merge-side-effects JSON. Python 652/Web 177 tests, lint/build passed; no production verification claimed.
- Stop after PR CI for merge authorization. No automatic Worker deployment or migration for this increment. Lifecycle, shared-account evidence rollover, full-task bounds and remaining adapters precede live acceptance; `080-RUNNER` remains incomplete.

| Stage ID | Scope | Code status | PR / HEAD / merge SHA | Deployment | Runtime / data evidence | Blocker | Next action | Authorization needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `051-A` | Non-interfering source observation error boundary | `MERGED_DEPLOYED` | PR #130; merge `1894f0588efb4665f847ba4aebd2f6401164de9e`; normalized-evidence ordering follow-up PR #137, merge `6d7c5a39bc1731ac32236200aefd53bcbee86e28` | PR #137 deploy run `36874232795` succeeded; Worker build identity verified | Receipt creation follows normalized evidence verification; source fingerprint v0.5.1 includes normalized identity; flags unchanged | None for this item | Keep sidecar failures non-fatal only for classified observation storage errors | — |
| `051-B` | Probe OFF/OBSERVE/ENFORCE contract and date coordination | `MERGED_DEPLOYED` | PR #131; merge `5cfd2dd8d301bf6e10daf81f6806173742ec2584`; main CI `36853968085` succeeded | Deploy `36853967739` succeeded; production identity verified | `MARKET_DAY_PROBE_ENABLED=0`, no runtime observation or collection dispatched; no migration pending | None for this item | Implement `051-C` code only; keep PCF read flag off | Merge/deploy authorization after CI is green |
| `051-C` | PCF capture and qualification separation | `MERGED_DEPLOYED` | PR #132; merge `f1d1741947535b8589fa762dfb91939a6920f6d2` | Deploy run `36858489158` attempt 2 succeeded; main CI `36858490693` succeeded; Worker version `f4f7fab0-ea72-44e2-b353-ebede8beaa33` | Runtime flags unchanged (`SOURCE_OBSERVATION_SHADOW_ENABLED=0`, `MARKET_DAY_PROBE_ENABLED=0`, `PCF_CAPTURE_V2_READ_ENABLED=0`); no migration pending; no collection/PCF run | None for this item | Implement `060-A`; keep new executor metadata additive and production entry unchanged | — |
| `060-A` | Unified application checks and checkpoints | `MERGED` | PR #133; squash merge `07c06ce8278ed27895f9334319e2bd71fb472042` | No deployment required; no workflow or production entry change | Shared identity/plan/checkpoint/retry seam is on `origin/main`; legacy collect/publish/recovery wrappers preserved; CI passed | None | Implement `060-B`; keep budget admission candidate-only | — |
| `060-B` | Unified budget and measured/estimated/unknown accounting | `MERGED` | PR #134; squash merge `8258d779b5ca7affce6895e4bdb729bf871c7f70` | No deployment required; no workflow or production entry change | Unknown-aware vectors, conservative admission, atomic reference ledger and stage-report basis changes are on `origin/main`; CI passed | None | Implement `060-C`; keep candidate entry disabled for production | — |
| `060-C` | Thin workflow and local runner entry points | `MERGED` | PR #135 merge `d21d70c826d4faf02c3fa887244bc3babe2edefb`; PR #136 merge `f01678e5eb68a06cf57f73a3d3864bc371905be1` | No Worker deployment required; candidate workflow remains manual-only | Candidate CLI/runner validates exact SHA, dependency lock, auth, lease and recovery envelope; candidate entry remains outside production schedule/read path | None for this code item | Continue with `070-A`; do not use the manual candidate entry for production without its own authorized canary | — |
| `070-A` | Capability contracts and immutable candidate manifest | `MERGED` | PR #138 merge `0052e7225805f0445f6090a1dce4ad769dd61e84`; PR #139 merge `e7868f91099406b6173e5f61a118fcfbee01f594` | No Worker deployment required for PR #139; it changed offline producer/slot validation only | Empty-sample READY claims are rejected; the complete v4 contract and every artifact are validated before slot projection; precise period evidence reasons are preserved. v4 remains opt-in/candidate-only; no production candidate was uploaded and no read path changed | None for code item | Implement `070-B`; keep all production data and read paths untouched | — |
| `070-B` | Period dependency and historical repair planner | `MERGED` | PR #140; merge `8769d93485af63d751acae9936f29b103c18d87e` | No deployment; PR changed no `web/**` runtime files or deployment workflow | Bounded offline plan defines official-session baselines, all supported windows, raw→normalized lineage, formula/classification versions, exact gaps and minimum downstream rebuilds. CI passed. No production backfill or remote operation | None for this code item | Implement `070-C`; keep production reads unchanged | — |
| `070-C` | Candidate UI/API and period coverage states | `MERGED_DEPLOYED` | PR #141 / #142 contained in main; current deployed main `d8cb795b6f2e083874af092bd3a112d80bbf59aa` | Deploy run `36993056006` checked out current main and verified served build identity; no migrations to apply | Candidate-only UI/API remain separate from active reads. No candidate upload or read switch occurred | Candidate/UI/data and Access acceptance not performed; earlier browser extension issue remains skipped per user direction | Prepare bounded OBS-CORE only when its evidence, budget and runtime authorization are available | Separate candidate/canary runtime authorization |
| `OBS-COORD` / `OBS-PCF` / `OBS-CORE` | Independent real-day observation lines | `NOT_STARTED` | — | — | — | Each has its own code and deployment prerequisites | Submit an explicit bounded execution package when eligible | Separate runtime authorization |
| `080-RUNNER` | Real adapters, durable resume and lease-safe runner canary prerequisites | `CODE_IN_PROGRESS` | PR #143 merged `75dce07761cb79fc1692b4be1bd2b1057806e58d`; PR #144 merged `d8cb795b6f2e083874af092bd3a112d80bbf59aa`; current client branch `codex/080-runner-heartbeat-20261002` | PR #144 deploy `36993056006` succeeded with exact build SHA; no pending migration. This Python client increment requires no Worker deployment | Bounded core adapter and ordinary renewal endpoint are on main. Current client verifies real authority and checks/renews at boundaries; CLI/workflows remain fixture-only; no live canary | Durable cross-job checkpoint storage, remaining shared adapters, recovery integration and canary/budget package remain incomplete | Complete client PR/CI, stop for merge authorization, then continue durable resume prerequisites | Merge approval; all live execution/cutover separately authorized |
| `080-COORD` / `080-CORE` / `080-PCF` | Coordinator/core/PCF production cutovers | `NOT_STARTED` | — | — | — | Their independent observation evidence and explicit cutover authorization | One bounded canary and ten-day acceptance per capability | Separate production authorization |
| `090-*` | Bounded history repair and compatibility retirement | `NOT_STARTED` | — | — | — | Cutover evidence | Plan and execute one bounded batch at a time | Separate recovery/cleanup authorization |
| `100-ACCEPT` | Stable operations and final product sign-off | `NOT_STARTED` | — | — | — | All relevant SLO evidence | Twenty consecutive real business days | Final sign-off |

## Current handoff block

### 2026-10-03 shared budget next increment

- PR #152 merged as `7ff01e17eda95e57a4da8eff363b5e10a121f1f2`; main CI `37086304887` passed. Python-only local persistence required no Worker deployment.
- Fresh isolated worktree `/private/tmp/flow-080-shared-budget-20261003`, branch `codex/080-shared-budget-20261003`, based on that main.
- Candidate shared transition protocol, additive account schema and bounded Python adapter implemented; parity, fencing, ambiguity and 20-client local concurrency tests added. No production admission or account availability is claimed.
- Details: `2026-10-03-080-shared-budget-contract.md`; merge deployment/migration side effects recorded separately. CI awaits the standalone PR. No production execution, flags, remote migration or protected-file changes performed.
- Next: full local/hosted CI, then stop for merge/deployment authorization. Evidence provisioning/rollover, lifecycle, full-task bounds, recovery and live acceptance remain incomplete; `080-RUNNER` remains CODE_IN_PROGRESS.

### 2026-10-03 checkpoint pre-spend allowance (current)

- PR #150 merged `ef999521db5fed3271b87679f2f6e6b81b2e7878`; main CI `37030459135` passed. No Worker deployment required.
- Fresh worktree `/private/tmp/flow-080-budget-io-20261003`, branch `codex/080-budget-io-20261003`, based on that merge.
- Candidate checkpoint transport consumes allowance before I/O through the existing atomic reference ledger. Ambiguous readbacks cost allowance too; commitments and physical measurements are separate.
- Details: `docs/audits/2026-10-03-080-checkpoint-budget-io.md`. This does not prove shared durable account reservation, lifecycle or production costs; evidence labels the in-process scope.
- No production wiring, schema, flags, active pointer, retention or collection changes; original protected paths untouched. PR/main CI only, no Worker auto-deploy/migration.
- Next: stop at CI-passed merge authorization, then persistent shared resource and lifecycle prerequisites. `080-RUNNER` is still incomplete.

### 2026-10-02 budget ledger safety increment (current)

- PR #149 merged as `2c110962befca2b8788638e3688a2e04e1d9dc83`; main CI `37028156674` succeeded. No Worker deployment was required.
- Fresh worktree `/private/tmp/flow-080-budget-20261002`, branch `codex/080-budget-20261002`, based on that merge.
- Before wiring candidate resource accounting, fix confirmed reference-ledger defects: expiry checked at execution/report methods, physical debit requires measured basis, final settlement cannot erase observed cost, measured aggregation preserves conservative freshness.
- Details: `docs/audits/2026-10-02-080-budget-ledger-safety.md`. Full local/hosted results in PR; no production verification claimed.
- Shared durable reservation integration and checkpoint lifecycle remain incomplete. This is not a production budget ledger or completed `080-RUNNER`.
- Only Python/tests/docs changed; PR/main CI, no automatic Worker deploy/migration, no live task or protected original-file edits. Stop after CI for merge approval, then continue those prerequisites from latest main.

### 2026-10-02 offline checkpoint preflight increment (current)

- PR #148 merged as `979115dfb20c52fdf3ad5941068a03c07d0d6fa0`; deployment `37024110459` succeeded, exact merge build identity verified, no migrations to apply. Worker version `bf4b5ccf-3262-4ffb-9456-764c2e44b06f`.
- Fresh worktree `/private/tmp/flow-080-admission-20261002`, branch `codex/080-admission-20261002`, based on that merge.
- Offline preflight reuses bundle verification and resource accounting, reports actual encoded/decoded bytes, transport fit and complete-budget diagnostics. It grants no execution authority, creates no reservation, and does not pretend lifecycle policy is installed.
- Existing config has no R2 Class A/B caps; those remain UNKNOWN. No provider quota, real core artifact size or lifecycle acceptance has been measured in this increment.
- Details: `docs/audits/2026-10-02-080-checkpoint-preflight.md`. Python tests: 586 passed. Web verification and hosted CI recorded in the PR.
- Only Python/tests/docs changed: no Worker deployment or migration on merge, no production reads, flags, active pointer, collection or original protected files changed.
- Next: full CI then stop for merge authorization. Runtime lifecycle, shared reservation integration, full-task cost evidence, larger-artifact strategy and remaining adapters still precede a separately authorized canary. `080-RUNNER` is not complete.

### 2026-10-02 bounded R2 transport increment (current)

- PR #147 merged as `41d4a85187bf9df1671c42530c2f55d90273418a`.
- Worktree `/private/tmp/flow-080-r2-20261002`, branch `codex/080-r2-20261002`, based on that merge.
- Candidate runner now supports explicit trusted digest references through bounded admin R2 transport; ordinary owner/attempt authority reuses pipeline_window. No production caller is enabled.
- Details and actual deployment side effects: `docs/audits/2026-10-02-080-checkpoint-r2.md` and its merge-side-effects JSON.
- No new migration, but web changes trigger automatic pending migration apply + Worker deployment on merge. Current production pending set/account quota/Access acceptance remain UNKNOWN.
- Before live canary: lifecycle/resource admission, larger-artifact strategy, remaining adapters/recovery and rollback package. This stage is not production-complete.
- Next after full tests/CI: stop for explicit merge/deployment-side-effect authorization, then verify deployment before further candidate runtime work.

### 2026-10-02 lease-safe resume increment (current)

- PR #146 merged as `df47766f2539c376f468964b40f699089d953e6e`; bounded bundle contract is on main.
- Worktree `/private/tmp/flow-080-resume-20261002`, branch `codex/080-resume-20261002`, based on that merge.
- In-process candidate runner implements explicit bundle export and isolated restore under verified lease authority, with adapter re-verification and dependency/recovery binding. CLI/workflows remain unchanged.
- Details: `docs/audits/2026-10-02-080-bundle-resume.md`. Local separate-directory fixture is not production evidence.
- Durable R2 transport remains unconnected, explicitly reported as such; no production or recovery run is authorized.
- PR/main CI only; no Worker deployment, migration, flags, active pointer or original protected files changed.
- Next after CI and merge approval: bounded authenticated immutable R2 transport. Remaining adapters, recovery/budget integration and live canary stay pending.

### 2026-10-02 checkpoint increment (supersedes client in-flight status below)

- PR #145 merged as `e72d2503fa12a61d72d828eb25b5653ddf5a496c`; ordinary lease client code is complete, not production-wired.
- Fresh worktree `/private/tmp/flow-080-checkpoint-20261002`, branch `codex/080-checkpoint-20261002`, based on that merged main.
- Current scope: bounded immutable stage bundle format and pure verification, using the existing application checkpoint contract. This is a prerequisite, **not completed durable resume**.
- Local Python suite: 553 passed (including 12 new bundle tests). Web verification follows the existing lint/test/build chain.
- Details and remaining ordered tasks: `docs/audits/2026-10-02-080-checkpoint-bundle.md`.
- No remote operations, production entry, flags, D1 schema, workflows or active pointer changed. No Worker deployment side effect; PR/main CI consumes Actions minutes.
- Next: after this isolated PR/CI and merge authorization, implement lease-safe import/export integration and bounded R2 transport. No live canary is authorized.

- **Last checked:** 2026-10-02; the roadmap revision in the user-provided handoff is the controlling implementation order.
- **Original repository:** `/Users/xiaoker/Documents/交易台` (dirty user worktree; read-only for this task).
- **Current isolated worktree:** `/private/tmp/flow-080-runner-heartbeat-20261002`.
- **Branch:** `codex/080-runner-heartbeat-20261002`.
- **Base:** fetched `origin/main=d8cb795b6f2e083874af092bd3a112d80bbf59aa`; PR #144 merged/deployed and exact served build identity verified via deploy run `36993056006`.
- **Completed before this stage:** `051-A` through `051-C` merged; `060-A` through `060-C` merged (PR #135 and #136 complete `060-C`); PR #137 corrected source-observation receipt ordering and deployed. Observation/probe and PCF read flags remain unchanged/off.
- **Current in-flight work:** ordinary-run lease client and cooperative heartbeat prerequisite. PR #144 deployment includes all preceding main code, including `070-C`; the current served code identity is verified, but actual candidate/UI/data and Access acceptance remains unperformed. No remote run/lease was created by this phase. Current production flags and account quota remain UNKNOWN.
- **Effective authorization:** continue the isolated 080-RUNNER code PR and its CI. Do not merge, deploy, upload a candidate, contact official sources, change a flag, or run a canary without the corresponding explicit authorization.
- **Local verification:** complete Python suite 541 passed; Web build/tests 159 passed; lint and diff checks passed. Hosted CI is pending PR creation. The localhost HTTP test requires local socket binding (sandbox default denied it; isolated authorized test run succeeds). No production test was substituted with a fixture.
- **Merge side effects:** this PR is limited to `src/**`, `tests/**`, and `docs/**`; it does not match the `deploy-worker.yml` `web/**` path filter and has no migration. A pull request runs `.github/workflows/ci.yml` (Python and Web build/tests/lint) and branch-freshness CI, consuming Actions minutes. The earlier D1 pre-merge check for PR #141 is not evidence of the current deployed version.
- **Code review:** completed manually against `docs/code-review-checklist.md`; that checklist is a review aid, not a new approval or execution gate. The optional GStack review bundle is absent and does not block this review. Screenshot-based browser inspection remains unperformed.
- **Next allowed action:** push the standalone client PR, wait for CI and stop for merge approval. After merge, continue durable checkpoint transport and remaining stage adapters using existing application/receipt APIs. Recovery approval integration, resource reservation and single-window canary/rollback still need completion; no production activation is authorized.
- **Forbidden in this work:** wiring new code to production scheduling/publishing, candidate upload/activation, production read-path switch, feature-flag changes, collection, recovery, history repair, active-release mutation, or destructive cleanup.
- **Protected paths:** `reports/phase0/latest-*`, `memory/`, and all other pre-existing user changes in the original worktree are untouched.
# 2026-10-03 local budget persistence increment

- PR #151 merged as `48d1e6b86931b41d50d54ff63e2d589a4b1fa177`; main CI `37084824734` succeeded.
- Fresh branch `codex/080-durable-budget-20261003` from that merge. Local-only SQLite adapter reuses existing budget transitions under bounded atomic journal replay; restart and separate processes sharing one file cannot forget allowance.
- See `2026-10-03-080-local-budget-persistence.md` for trust boundary, limits and tests. No remote shared quota store, production caller, migrations or workflow changes.
- `080-RUNNER` remains CODE_IN_PROGRESS. Cross-host/account-wide reservations, full cost evidence, checkpoint lifecycle, remaining adapters, recovery and real canary remain required.
- Stop after hosted CI for merge authorization. Original reports/latest, memory and user edits untouched.
