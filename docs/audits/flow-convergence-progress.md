# Flow convergence progress

This is a human-readable handoff index for the roadmap at
`docs/handoff/2026-09-23-flow-target-architecture-and-convergence-roadmap.md`.
It is not a production gate, database protocol, or replacement for GitHub,
R2, D1, release, or receipt evidence.

## Current stage

### 2026-10-13: 080-CANARY prerequisites / provider-period contract repair

- Base main `e3633bf2f221d2680adbcc0182d03ade2d4b7214`; PR #183 merged with explicit authorization. Main CI `38345547463` and automatic deploy `38345548001` passed; served SHA matches merge, Worker `e6d8691f-97e4-40c6-8732-5da5e62d1d8e`, D1 `No migrations to apply!`.
- Official account login/read-only usage inspection succeeded. Actual R2 usage periods follow a subscription cycle, while prior reconciliation uses calendar epochMonth. Rounded UI counts and GB-month storage are not exact current counters/bytes. No private financial values or exports included here.
- Current worktree `/Users/xiaoker/.codex/worktrees/080-budget-periods/交易台`, branch `codex/080-budget-period-contract-20261013`; no open PR at start. Reuses existing account/endpoint/CAS/auth and preserves UNKNOWN holds, cumulative task counters and receipts. Explicit evidence v2 adds per-dimension UTC day/calendar month/billing cycle/instant precision contract; legacy behavior is retained, upgrade is explicit and downgrade rejected. Details/merge effects: `docs/audits/2026-10-13-080-budget-period-contract.md`.
- State LOCAL_VALIDATION_COMPLETE / DELIVERY_AUTHORIZATION_PENDING. Final `npm run lint` and `npm test` passed build plus 235 tests; then root `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -o addopts='' -q --tb=short` passed 1111 tests in 52.25s. Seven new behavior scenarios include SQLite concurrency and real admission transitions. Same legal fixture evidence returns 422 on prior main and a read-only prepared response (writes 0) on the fixed handler. This is local evidence, not production usage or acceptance. No production claim, account dry-run/apply, recovery approval, workflow dispatch, collection, canary, flags or active-pointer mutation.
- Independent states: components merged YES (PR #181/#182/#183); complete local/hosted call passed YES for prior versions; formal entry code merged YES but true execution readiness blocked on valid account evidence/authority; real canary NOT RUN; production switch/ten-day acceptance NOT RUN.
- Next allowed action: authorization for one repair PR/initial CI requested (4–8 Linux runner minutes estimate, not measured); previous one-CI authorization was consumed by PR #183. After delivery/CI, stop for merge/deploy authorization. Then obtain exact truthful current provider evidence and prepare a bounded account dry-run. Actual initialization/apply and one real candidate need separately disclosed authorization. This repair is not telemetry acquisition or CANARY_PASSED.
- Original `reports/phase0/latest-*`, `memory/` and user edits unchanged. Prior persistent worktree's local merge/evidence notes retained, not overwritten or separately pushed. No additional ledger, workflow, migration, dependency upgrade or manual JSON gate.

## Historical implementation notes

### 2026-10-13: 080-CANARY prerequisites / budget readiness delivery

- PR #182 merged `56e8a8d336eaa63693238ca09b5529335173d11f`; main CI `38282712426`, deploy `38282713110` passed; exact served SHA verified, no migrations to apply. Real candidate and production switch NOT RUN.
- Bounded source verification: artifact `11721908335`, Collect `38193100238`, snapshot SHA256 `908ff478a6fced6ae044f9ff29f2e20c1b3d7fd191b7f76f3ada73abe9b18da1`, 2173496 bytes, confirmed asOf `2026-10-09`. No recollection. Exact-key production reads (read 1/write 0) show source lease COMPLETED, account flow-account-v1 absent. Current official usage UNKNOWN.
- User authorized synchronization of three affected tests and one PR/initial CI. Existing endpoint/row/auth now has opt-in account_reconcile, dry-run, insert-if-absent, CAS, stable replay, measured evidence and preserved UNKNOWN/counters/receipts. Fixed R2 caps added to existing policy; unknown-policy tests retain fail-closed assertions. No production initialization, lease/approval, collection, flag or active-pointer mutation authorized or performed.
- Temporary uncommitted worktree was removed before shipping. Restored from the recorded patches to persistent managed checkout `/Users/xiaoker/.codex/worktrees/080-budget-readiness/交易台`, branch `codex/080-budget-readiness-delivery-20261013`, latest main `56e8a8d`. Fresh restored-source verification: Web lint/build plus 228 tests passed; then complete root Python 1111 passed in 51.50s. Independent coverage/plan audit found no blocker; six requested coverage areas were added (31 narrow budget tests pass). Prior results are historical, not reused as proof. User authorized one PR/initial CI; next delivery state is CI_PENDING, not merge/deploy authorization. Guide/effects: `docs/audits/2026-10-13-080-budget-readiness.md`. No report-only CI or helper platform.
- Next: one coherent PR/necessary initial CI, then stop for merge/deploy authority. Actual official evidence, account initialization and legitimate execution authority must precede the separately authorized bounded candidate. STAGED may supply candidate evidence while accounting UNKNOWN keeps the hold; neither production acceptance nor task completion is implied.

Prior entries are historical.

### 2026-10-12: 080-ENTRY / r4 — LOCAL_VALIDATION_COMPLETE / DELIVERY_PENDING

- 2026-10-13 delivery authorization received: one ENTRY PR and its necessary initial CI only; merge, deployment, production candidate and canary remain unauthorized. Delivery verification re-runs the complete Python suite, Web build/test/lint and named-file scope check. Ship coverage audit identified optional rejection-branch gaps; 15 bounded metadata/ZIP/runtime/policy probes were added before the initial PR, not by repeatedly spending CI minutes. Plan audit found the nine ENTRY code-contract requirements addressed; hosted Linux execution remains for this CI and production acceptance remains separate.
- Delivery verification results: Python `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -o addopts='' -q --tb=short` passed **1111 tests in 52.17s**; Web `npm test` passed build plus **220 tests**, `npm run lint` passed; diff check passed. Base remains `087a6ad`; original dirty worktree status is unchanged. The new ENTRY implementation is not yet merged; candidate calls are locally verified only, hosted Linux/real canary/production switch remain pending/not run.

- 2026-10-13 continuation: final entry/runner/workflow targeted regression passed (53 tests), including the last sanitized local-error handling; diff check passed. No commit/push/PR/hosted CI or production action yet. Seven-artifact policy examples reserve the whole two-attempt scope: ordinary 508 Worker requests / 13,312 D1 reads / 6,784 writes; recovery 740 / 20,736 / 14,208. Both include 20 Actions minutes, 87,938,176 R2 bytes, 150 Class A and 1,618 Class B operations. These conservative examples are not actual usage or remaining quota. Next action is one code PR plus its necessary initial CI after bounded CI authorization when official Actions capacity remains unavailable; no merge/deploy/canary implied.

- Latest continuation: fixed candidate policy now derives complete seven-dimensional reservation from actual artifact count and the existing bounded request profiles. First and second attempts request the identical two-attempt total, avoiding a takeover enlargement conflict. Bounds include indexed D1 probes/index writes, recovery plan reads, immutable intent/artifact/manifest effects, checkpoint objects and the complete manual job timeout. These are reviewed conservative estimates, not official account telemetry.
- Formal CLI and the same single manual workflow now assemble real candidate mode as well as explicit fixture/dry-run. Workflow source preparation reads one exact collector artifact (three bounded GitHub reads, max 6 MiB), validates namespace/run/expiry, forbids unsafe ZIP entries, selects one snapshot and verifies target date/hash without extracting other sources. Tokens are not forwarded to the signed archive host; source data and whole state directories are not uploaded. Explicit bounded private budget/intent journals are retained on failure so ambiguous operation IDs are not lost with the runner.
- Local ordinary/recovery scenarios now cover actual CLI parsing/call with only the trusted localhost/clock seam overridden, and abrupt termination before publish followed by real owner claim and a separate Python process using the original budget journal plus exact handoff. All reach candidate STAGED without duplicate imports/reservations or active-pointer change. Physical provider usage remains UNKNOWN; CLI exits nonzero, hold/counters persist, and this is not a real canary or production completion. Actual SQL query plans recorded by the existing bounded same-Worker fixture have indexed searches and no data-table scan.
- Final local verification: complete Python regression passed (1095 tests, 50.69s), plus all four manual-workflow tests after the added shell syntax/input-interpolation check; Web `npm run lint` passed; `npm test` passed including production build and 220 tests. Independent read-only review found and rechecked two repaired gaps: Python runtime now uses a complete 19-package pinned CPython3.12 closure, required in the identity with the Web lock and verified against installed versions; workflow dry-run validates the actual declared request instead of silently using an unrelated fixture. Fixed wheel availability was verified read-only for macOS and Linux/CPython3.12 (not Linux execution proof). No remaining concrete blocker was reported by that review. A legacy install-order assertion was updated to test the new locked installation before CLI execution, not removed or weakened.
- Delivery boundary: local code/behavior is validated, but no PR/hosted Linux CI or production invocation/merge/deployment has occurred. Official Actions capacity remains UNKNOWN. Creating a PR would run two bounded Flow CI jobs plus Branch freshness (no explicit timeout); future merge also triggers main CI and automatic Worker/all-pending-migration deployment due to the Web fixture path. Exact effects are in `docs/audits/2026-10-12-080-entry.md`. Await a bounded initial-CI spending authorization if remaining capacity cannot be verified; do not infer unlimited minutes or production authority.

Earlier implementation notes below are chronological, not current readiness claims:

- Base: `origin/main=087a6addf5720c34bfa918065d7a8020e7fa5bf5`, PR #181 merged after explicit authorization. New worktree `/private/tmp/flow-080-entry-20261012`, branch `codex/080-entry-20261012`. Original dirty worktree and prior worktree's progress-only update are preserved.
- 080-FLOW delivery verified: initial PR checks and main CI run `38219370841` passed; deploy run `38219371948` passed in 2m8s. D1 output: `No migrations to apply`. Worker version `87af36d0-1fa9-4d70-b7c4-a332e2ece727`; the authenticated deployed-build check confirmed merge SHA `087a6ad` on attempt 2. This verifies deployment identity, not a real candidate task/canary or UI acceptance.
- Existing components: 080-FLOW runner, same ordinary/recovery lease, stable shared budget, durable operation/intent stores, real core adapters and checkpoints are SATISFIED_EXISTING. No new ledger, lock, endpoint, scheduler or business formula is needed.
- Confirmed connection gap: `trading_desk.cli pipeline` and the single `pipeline-executor-candidate.yml` currently expose only fixture execution. They do not assemble the trusted live candidate clients/adapters/budget lifecycle or explicit fixture/dry-run/candidate modes.
- Minimal implementation scope: one trusted entry assembly reused by local CLI and the existing manual workflow; fixed SHA/lock/input validation and environment-only credentials; ordinary/recovery scope preserved; reuse existing transports and exact archived source/plan evidence. No arbitrary Python module/class input. No YAML business state machine.
- Integration target: command-level fixture/dry-run without remote effects; actual assembled ordinary/recovery candidate path against the local same-Worker HTTP/SQLite fixture; invalid SHA/lock/date/hash/mode/approval/budget fail closed; secrets absent from CLI/log/report/artifact. Keep default fixture behavior compatible.
- First implementation slice: `pipeline_entry.py` provides bounded/symlink-safe request loading, recursive inline-secret rejection, exact real git checkout SHA verification and reuse of existing identity/lock/stage-plan semantics. CLI now exposes explicit fixture/dry-run behavior; fixture remains the default. Dry-run returns DECLARED_INPUT_CHECKED with liveExecutionAllowed/serverAuthorityVerified false, performs no stages/network/checkpoint/report writes, and is not a candidate completion or grant. Ten new entry tests plus nine existing runner tests passed; diff check passed. No PR or hosted CI started. The live candidate assembly, clean-code/SHA gate, ordinary/recovery environment authentication, whole-task cost source and single-workflow wiring remain unfinished in this same stage; do not ship this slice alone.
- Candidate assembly continuation: fixed build/verify/publish graph is derived using the existing builder/verifier digest semantics from an exact pinned snapshot; supplied graphs must match, fixture stageResults cannot enter candidate mode, and dates are not rewritten. Both ordinary/recovery clients, shared budget, durable journals, immutable candidate intent and checkpoints now assemble through one `run_candidate_entry` function. Existing same-Worker HTTP/SQLite fixture tests prove both paths reach a STAGED candidate with no active-pointer change; unavailable provider measurement yields UNKNOWN and retains allowance rather than inventing debits. Environment-only credentials are not exported; pinned sources with inline credentials are rejected before becoming checkpoint artifacts. Unknown task cost rejects before journal/network effects. Fresh/second-attempt bounds and required retained resume budget journal are explicit. 19 entry tests plus nine existing runner tests passed; production build and diff check passed. Actual CLI candidate policy sourcing, complete negative/resume command cases and the single manual workflow still remain; no partial PR/CI or production invocation was made.
- Not doing: production dispatch, collection, recovery registration, flags, active pointer, deletion, PCF v2, canary or production switch. No additional CI or PR has been triggered for 080-ENTRY. Official remaining account capacity is UNKNOWN.
- Entry identity hardening: SHA and clean-code Git checks now isolate repository-overriding `GIT_*` variables without modifying the caller environment. A separate temporary-repository test proves overrides cannot hide unreviewed code. Request read errors are sanitized without echoing paths or exception text. Entry/runner targeted regression passed (30 tests), followed by the two final hardening tests after fixture isolation. Full Python regression (`PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q --tb=short`) passed with exit 0 on the final files; diff check passed. Fixed repository policy currently lacks a complete seven-dimensional candidate upper bound; platform limits and stage allocation targets are not substitutes. The candidate CLI/workflow and cost-policy evidence remain unfinished, not production-ready.
- Independent states: components merged **YES**; full local/CI call passed **YES**; formal entry runnable **NO**; real canary **NOT RUN**; production switch/ten-day acceptance **NOT RUN**.
- Next allowed action: implement and test the trusted entry in this same new worktree; disclose actual merge effects and stop for its own merge authorization after required CI. No need to ask whether to start this code phase.
- Manual entry continuation: the existing single manual workflow now explicitly selects fixture or dry-run (fixture default), passes the choice through a quoted environment value, and rejects unknown modes. Dry-run must produce DECLARED_INPUT_CHECKED, no server authority and no state/result files. Evidence upload is narrowed from a directory wildcard to summary/result only; journals, sources and checkpoint contents cannot be exported by that wildcard. This is local validation wiring, not a real candidate execution entry. Candidate cost-policy derivation and live CLI/workflow wiring remain pending in this same PR scope. No workflow was dispatched.
- Candidate CLI now assembles the same fixed entry with explicit pinned source, retained budget journal, optional recovery plan and resume hash; credentials remain environment-only and the loopback fixture seam is not a CLI option. Policy is read exclusively from tracked `config/resource-budget.json`, with exact seven-dimensional vectors and explicit reviewed-upper-bound evidence metadata; no request-supplied policy path is accepted. Daily platform limits cannot substitute for task costs. Current repository has no proven candidateEntryPolicy, so production entry remains blocked with `entry_cost_policy_not_established` (dirty code is also rejected). This is intentional incomplete readiness, not a runnable-production claim. Targeted entry/runner/workflow tests and two new policy/CLI rejection tests passed; diff check passed. Complete cost derivation, resume/negative command coverage and real workflow input transport remain to close before delivery; no PR or production run started.

## 080-FLOW pre-merge implementation notes (historical evidence)

### 2026-10-12: 080-FLOW / r4 — delivered by PR #181

- Current base: fetched `origin/main=2850aa733052b28200dac6eb6ebaa449bd1da3d4` (PR #180). No open PR was returned by the repository query at this check.
- Isolated worktree: `/private/tmp/flow-080-full-lifecycle-20261011`; branch: `codex/080-flow-full-lifecycle-20261011`. Original dirty worktree remains read-only.
- Current uncommitted implementation: explicit `flow-budget-task-v2` business identity in the existing account ledger; atomic current-owner adoption preserves reservation/consumed/authorized counters; old owner and changed input are rejected; UNKNOWN remains held. Legacy contract behavior is retained, and existing legacy holds are not silently converted.
- Candidate client now reuses verified server revisions and retains one pending operation in bounded SQLite storage across process restart. Only an explicit pre-commit revision conflict permits one refresh with the same operation ID. Lost replies do not automatically retry or become execution permission.
- Existing runner now has an opt-in shared-budget lifecycle: stable input binding, current-owner adoption, reserve/RUNNING, per-stage authorization/debit, and final measured or UNKNOWN settlement. Unknown settlement is not reported as overall COMPLETED. Default callers remain unchanged; no production caller was added.
- Local evidence: server tests include 20 concurrent adoption requests with one winning CAS. Real HTTP/SQLite tests cover ordinary/recovery budget lifecycle and a real committed grant reply discarded before client receipt/restart. Six-stage boundary integration retains fixture stage adapters. A new built-Worker HTTP test uses actual ordinary lease, build/verify/publish adapters, candidate/checkpoint clients, SQLite and a bounded local R2 substitute; Access uses signed fixture JWT/JWKS with the production verifier. It proves the legacy 32-request lease ceiling blocks the full chain while retaining one hold and leaving active pointer unchanged. This is NOT successful full-task completion or provider quota proof. Latest complete Python suite and Web lint/build/test (219) passed.
- Component merged: preceding 051–070 and 080 prerequisites are SATISFIED_EXISTING; **this branch is not committed, pushed or merged**.
- Full call passed: **LOCAL ordinary/recovery build→verify→candidate paths and all three completed stage-boundary resumes passed**. Separate processes restore immutable remote handoffs with one reservation and no duplicate candidate imports. Injected known test measurements cover SUCCEEDED settlement; missing provider usage ends UNKNOWN and retains the hold. UNKNOWN effects remain blocked on takeover. Seven-dimension declared cost admission and the bounded fault envelope are implemented and reviewed; hosted CI remains blocked by unknown Actions capacity. No production caller is enabled.
- Request-envelope follow-up: existing offline preflight counts **65 lease + 37 budget + 21 candidate + 6 checkpoint = 129 Worker requests**, verified against built-Worker/SQLite/R2. The former private-limit test instrumentation was removed. `FreshCandidateRequestProfile` is an opt-in, input-bound, ordinary-only three-stage/both-export policy with a maximum 300-second deadline, one checkpoint readback and one candidate reconciliation, one additional failure-tail settlement, one revision refresh/retry per mutation, and conservative renewals at the guard's 15-second minimum spacing. Seven artifacts derive ceilings **89 / 118 / 22 / 7 = 236**; old client defaults remain **32 / 32 / 40 / 12**. Unknown envelope inputs remain unknown in generic diagnostics. Existing reservations, resume inputs, recovery mode, insufficient control-cost reservation and mismatched artifact count cannot use this fresh profile. Server resource admission, CAS/lease fences and UNKNOWN holds remain unchanged; HTTP ceilings are not platform quota.
- Full HTTP fault evidence: a real concurrent SQLite account revision update causes the production handler's CAS rejection and only one bounded same-ID retry (+2 budget requests); an actual committed candidate reply discarded by the client leaves a durable pending intent and UNKNOWN reservation, with exactly one import request/no duplicate upload. Reopening the local store retains the pending intent. Understated-control and resume-scope cases fail before any control request, reservation or R2 write. A wrong artifact count rejects before candidate upload. None of this is real account telemetry or production validation.
- 2026-10-12 continuation: the previously quota-blocked tail-settlement patch is now tested. The runner's failure settlement only starts after its own reservation is admitted; a rejected fresh run against an existing RESERVED hold performs only its lookup and leaves status/cost unchanged (no adopt/settle/R2 effect). The committed-grant/lost-reply test now also uses a genuinely separate Python process to reopen the bounded SQLite operation journal, recover the original operation ID through the actual HTTP/SQLite budget handler, and return only not_authorized while retaining authorized/consumed/reserved counters. Full Python regression passed after these changes; latest Web build/test remains 219 passed and lint passed. No hosted CI or production validation was run.
- Recovery whole-call continuation: the local fixture now seeds an immutable canonical plan and ACTIVE approval solely into local SQLite/R2, while the actual Worker verifies Access, approval, lease, scope, dates, hashes and candidate/checkpoint requests. Normal recovery costs **130 / 37 / 21 / 6 = 194 Worker requests** for seven artifacts; bounded fault/renewal/tail ceilings are **198 / 118 / 22 / 7 = 345**. The exact approved plan must cover the entire 345-request envelope; an approval of 344 or a mismatched plan hash is rejected before HTTP. This neither registers a production approval nor changes any production flag. Legacy recovery clients remain unchanged.
- The profile supports the existing endpoint's 20-artifact schema ceiling with derived limits rather than silently preserving an insufficient 40-call candidate ceiling. Pure upper-bound tests give ordinary 470 and recovery 657 Worker requests at that maximum; these are estimates, NOT observed 20-artifact runs, free-account admission or provider usage. Seven-artifact actual-call evidence is separate.
- Actual full ordinary/recovery matrix: 22 HTTP/SQLite/R2 scenarios passed, covering normal flow, genuine CAS conflict, discarded committed reply, understated control cost, unsupported resume scope, wrong artifact count, existing hold, source hash mismatch, date mismatch and blocked QA. Error codes stay precise; negative source/QA cases never upload candidates or activate a release. Complete Python regression and web lint passed after this extension. Whole-run crash/takeover/resume remains the next same-PR target, not a new phase or helper PR.
- Second-attempt continuation (2026-10-12): explicit ordinary/recovery resume profiles require attempt 2, the exact remote handoff and a retained existing reservation; a new empty reservation or arbitrary local-resume input cannot substitute. Seven-artifact per-attempt ceilings are ordinary **103/136/22/11=272**, recovery **226/136/22/11=395**. Whole-task worker-request upper bounds retain the first attempt: **236+272=508** and **345+395=740**. Recovery approval and the task reservation must cover these cumulative bounds. Third attempts are not implicitly authorized by this profile.
- Actual continuation tests now use the existing real claim/recovery-claim endpoints after local expiry injection, then a separate Python process and a new job directory. The source file is removed: completed build output must be restored and revalidated from R2, not recomputed or recollected. There is one reservation with unchanged cost and inherited consumed/authorized counters, current owner/attempt 2, exactly one upload per candidate artifact, and no active pointer. A forced caller termination is simulated via SystemExit before debit; the resumed caller is a genuinely independent process. No actual production crash or canary is claimed.
- The pending-operation journal now mirrors only the owner learned from a validated server receipt. A higher owner/attempt can be recorded only after atomic adopt; stale clients cannot poison the current journal or settle. This is local pending-request integrity, not a second quota ledger or lease authority. Missing/corrupt journal metadata fails closed; no production journal exists for this unmerged opt-in implementation.
- UNKNOWN takeover tests preserve cost, authorized and consumed amounts exactly and issue no second candidate upload. Separately, known stage execution with delayed/unknown usage retains its full allowance (no invented zero debit), records stageUsageUnknown, and continues only under the original admitted reservation; an unknown total keeps final status UNKNOWN and the hold intact. Invalid measurement types remain errors. Current ordinary/recovery matrix is 28 cases; complete Python regression and lint passed.
- CI budget evidence: repository query reports PRIVATE. The authenticated billing summary request for 2026-10 returns HTTP 404 with an explicit missing user-scope message; no permission refresh, spending-limit change or CI was performed. Official Actions remaining capacity is UNKNOWN. This blocks CI-dependent external actions, not local implementation/review.
- Formal entry runnable: **NO**; real canary: **NOT RUN**; production switch/trading-day acceptance: **NOT RUN**. Current account budget/remote pending migrations: **UNKNOWN**.
- Seven-dimension cost continuation: the runner now aggregates stage estimates, actual connected candidate/checkpoint clients' worst-method estimates, explicit lease/budget request estimates and runtime before its first remote operation. Request profiles include bounded fault paths; second-attempt profiles retain first-attempt request costs and conservatively count stage costs for both attempts. Runtime Actions-minute bounds must cover the declared deadline for one/two attempts. Unknown cost stays UNKNOWN and rejects; inadequate R2 cost and unknown D1-control cost tests assert zero lease/budget/transport requests, reservations and R2 writes. Estimates are NOT physical usage, account capacity, or permission. The local fixture allowance was increased to cover its complete two-attempt envelope only; production caps/configuration are unchanged. Complete Python regression after cost integration passed; Web lint/build and 219 tests passed.
- Authority-loss continuation: local fixture fault injection expires the ordinary lease or revokes the recovery approval after build and before debit. The actual shared-budget handler must return `budget_lease_conflict`; no candidate/checkpoint upload occurs, no consumed cost is invented and no stale-owner settlement frees the RUNNING hold. This is local server-fence evidence, not a production revocation action.
- Final review continuation: reverse compatibility now also rejects legacy mutations against an existing stable v2 business hold, including a valid next owner, without changing legacy-only behavior or account state. New real HTTP cases restore completed build, verify and publish boundaries in separate processes with the same reservation; candidate imports occur only once. The publish-boundary resume uses the existing exact-STAGED readback/intent restoration facility, not upload permission. Known-settlement coverage uses an explicitly injected local test measurement; physical provider usage/account availability remain UNKNOWN. The fixture HTTP ceiling is derived from the two-attempt request profile plus 16 bounded fixture-only claim/fault/stats calls, with a hard 1000 ceiling; it is not a production limit. Latest web lint/build/test: 220 passed. Final complete Python regression after all additions passed; the actual built-Worker HTTP matrix contains 40 ordinary/recovery scenarios. `git diff --check` passed. This is local validation only, not hosted CI or production acceptance.
- Actual future merge effects are recorded in `docs/audits/2026-10-12-080-flow-merge-side-effects.json`: no new migration or production entry, but the existing web-path merge trigger deploys Worker/assets and checks/applies all pending remote D1 migrations. This disclosure is not another approval gate. No merge or deployment was performed.
- Shipping continuation: user requested continuation after the bounded one-PR/one-CI authorization request; only this PR and its initial required checks are in scope, not merge/deployment. Independent read-only coverage/plan audit found no concrete blocking defect; no invented numeric coverage rate. Pre-push trigger verification also found the existing `Branch freshness` Linux job, in addition to the two 10/15-minute CI test jobs; its workflow has no explicit job timeout, and the effects disclosure now includes it. Account capacity remains UNKNOWN; no spending-limit or permission-scope change is made. This repository has no VERSION/CHANGELOG/TODOS/CLAUDE metadata scheme; ship's optional project-bootstrap/version convention is not introduced into this stage.
- Next allowed action: after official budget evidence or explicit bounded CI authorization, commit only the listed task files, push this branch and create one 080-FLOW PR; run one necessary CI pass. Do not create a helper-only PR. The current CI has two Linux jobs with 10/15-minute timeouts; these are configured execution bounds, not official billing balances or a cost guarantee. CI success must precede the combined merge/deployment-effects approval report. Merge/deploy/production run remain unauthorized. CI builds the actual Worker before Python joint tests; this adds a build prerequisite/Actions runtime, not deployment. No 080-ENTRY work begins before authorized merge and required deployment verification.
- No production task, migration, deployment, flag, active pointer or protected original file was changed. Web changes imply automatic Worker deployment and pending-migration application upon a future authorized merge; no such action is authorized yet.

## Historical handoff records (not the current continuation instruction)

### 2026-10-11: candidate budget server-fenced checks

- Base main b1fa757 (PR #179); isolated branch `codex/080-recovery-lifecycle-20261011`.
- Explicit candidate mode removes redundant budget client fresh reads only; server atomic identity/approval CAS, due renewals and checkpoint fresh confirmations remain. Default unchanged.
- Two checkpoint operations: fixture 10 lease / 16 total requests versus legacy 26 / 32. This is not full runner lifecycle or account-budget proof.
- Web 215 tests, lint/build passed; Python and hosted results recorded in PR. Audit/effects: `2026-10-11-080-budget-fenced-checks*`.
- No production caller, flag, migration, pointer, collection or protected-file change. Main CI only on merge. Stop for merge authorization after CI.
- 080-RUNNER remains CODE_IN_PROGRESS. Next: integrate full runner-derived reservation/debit/settlement envelope and trusted entry before any separately authorized canary.

### 2026-10-11 dashboard recovery mapping

- Base main 2e2055b (PR #176); fresh codex/080-dashboard-scope-20261011.
- dashboard approval maps only to existing exact core build/verify adapters, with recovery guard and unchanged hash/date/coverage/confirmed QA. Existing checkpoint client/server share this mapping without granting other domains.
- Local Python 955 / Web 213 passed; build/lint passed. Audit: 2026-10-11-080-dashboard-recovery-mapping.md.
- Web merge triggers Worker deployment/pending migration apply; no new migration, flags, caller, pointer or collection change. Stop after CI for combined authorization.
- 080-RUNNER remains CODE_IN_PROGRESS; formal entry, cross-host handoff, full cost/lifecycle and real canary/rollback remain. Original protected paths unchanged.

### 2026-10-11 approved recovery checkpoint authority

- PR #173 merged `5f4da7e72e760b27ff8cbd773791a71ff9e6fe40`; main CI `38103045143` and deploy `38103045444` passed. Exact served build verified, no migrations to apply; Worker `de48c1cd-8b44-4cda-875a-600bec160c54`.
- Fresh `codex/080-recovery-artifact-20261011` implements one opt-in recovery checkpoint contract through the existing endpoint. Exact immutable approval, owner/attempt/date, joint lease/approval SQL and same shared budget; ordinary behavior preserved. Only proven core_collection/publish stage mappings permitted; no generic handoff/domain mapping invented.
- Audit/effects: `2026-10-11-080-recovery-checkpoint*`. No new migration; web merge triggers Worker deploy and pending migration apply (current pending UNKNOWN). Stop after final CI for combined merge/deploy/migration authorization.
- Full local Python 936/Web 207 passed; build/lint passed. No production caller, flags, claim, collection, approval, active pointer or original protected-file edits. Actual account availability UNKNOWN.
- Next: candidate publication recovery authority, exact domain mapping, trusted caller and full cost/lifecycle/canary/rollback. 080-RUNNER remains CODE_IN_PROGRESS, not recovery complete.

### 2026-10-11 recovery shared-account budget

- PR #172 merged `dac896b2fc69f6065ba0be707c5c92a7aee15f6b`; main CI `38067224021` passed; no deployment required for that Python-only PR.
- Fresh `codex/080-recovery-budget-20261011` implements one end-to-end client/server budget scope: existing ACTIVE recovery approval, immutable inline upper bound, atomic lease/approval CAS and cumulative attempt envelopes in the same account ledger.
- Audit/effects: `2026-10-11-080-recovery-shared-budget*`. No new migration, but web merge automatically deploys Worker and applies pending migrations (remote pending UNKNOWN). Stop after CI for combined merge/deploy/migration authorization.
- No production caller, flag, collection or active pointer changed. Actual account usage UNKNOWN. Recovery artifact authority, domain mapping, trusted caller, resource lifecycle and real canary remain; 080-RUNNER CODE_IN_PROGRESS. Protected original files untouched.

### 2026-10-11 recovery approval and lease client

- PR #171 merged `7494d9ce26cba61b4d6426f632cf99bac90f48a2`; main CI `38066630282` passed. No deployment required.
- Fresh independent `codex/080-recovery-lease-20261011` validates exact canonical approved scope, ACTIVE live approval and current recovery owner/attempt/date; renewals require fresh full approval reads. Shared lifecycle; no new lock or API.
- Audit/effects: `2026-10-11-080-recovery-lease*`. Python/tests/docs only; no migration/deploy, collection or production activation. Stop after hosted CI for merge authorization.
- Recovery-aware budget and artifact authority, domain mappings, trusted caller and real canary remain incomplete. 080-RUNNER remains CODE_IN_PROGRESS. Original protected paths untouched.

### 2026-10-11 exact recovery stage scope

- PR #170 merged `4e1f93348fae16b138aaa08917c3fa56baa8dae2`; main CI `38065767394` passed. No Worker deployment required.
- Same independent recovery worktree now closes a confirmed local scope gap: injected recovery adapters must receive and validate the approved plan before callbacks or state writes. Only identical stage semantics are accepted; product-domain mappings are not invented.
- Audit/effects: `2026-10-11-080-recovery-scope*`. Python/tests/docs only; no migration/deploy or production activation. Stop after CI for merge authorization.
- Full recovery lease/budget/transport integration remains incomplete; ordinary restrictions stay intact. 080-RUNNER remains CODE_IN_PROGRESS. Protected original files unchanged.

### 2026-10-10 completed publication checkpoint restoration

- PR #169 merged `a9878d47c9a78d55ab9c037ee766598eefc75dbd`; main CI `38064627849` and deploy `38064628262` passed. Exact served build verified; no migrations to apply.
- Independent branch `codex/080-publication-resume-20261010` adds explicit candidate-only reconstruction of current-owner local ACK records after all exact STAGED reads succeed. No upload permission is copied and pending writes are not erased.
- Audit/effects: `2026-10-10-080-publication-resume*`. Python/tests/docs only; no Worker deployment, migration or production registration. Stop after CI for merge authorization.
- Remaining recovery integration, trusted production channel, actual budget/lifecycle and canary/rollback. 080-RUNNER remains CODE_IN_PROGRESS. Original protected paths unchanged.

### 2026-10-05 exact checkpoint handoff

- PR #168 merged `814b0be96240b7497f6aa6e05be10e0dab8da917`; main CI `37216493324` and deploy `37216493673` succeeded, exact production build confirmed on attempt 2, no migrations to apply.
- Current independent branch `codex/080-checkpoint-handoff-20261005` adds bounded immutable confirmed-prefix locators through existing checkpoint transport; fresh hosts discover exact bundle references from one trusted handoff hash and verify every bundle before reuse. No bucket listing or mutable latest pointer.
- Candidate-only parameters; no CLI/workflow/scheduler production registration. Recovery and publish-journal restoration are not fabricated. Audit/effects: `2026-10-05-080-checkpoint-handoff*`.
- Stop after CI for explicit merge, automatic Worker deployment and pending migration check/apply authorization; no new migration, actual pending status UNKNOWN. Protected original files unchanged.
- Remaining: publish journal transfer/reconciliation, recovery wiring, trusted production artifact-channel integration and real resource/lifecycle/canary/rollback acceptance. 080 remains CODE_IN_PROGRESS.

### 2026-10-04 cross-host candidate upload intent

- PR #167 merged `c373e3bd440662bee76310ba168716a39003229b`; main CI `37201932146` and deploy `37201932265` passed; exact merge build verified, no migrations to apply.
- Current branch `codex/080-intent-handoff-20261004` uses conditional bounded R2 upload intents before candidate effects. A new host cannot infer upload permission from an older empty SQLite journal; duplicate attempts block and only exact readback resolves the local unknown hold.
- Legacy callers unchanged; no production runner registration, collection, activation, flag or retention change. Unfinished marker without an artifact remains blocked rather than retried.
- Audit/effects: `2026-10-04-080-cross-host-intent*`. Web changes trigger automatic deploy and pending migration check/apply; no new migration, production pending state UNKNOWN. Stop after CI for explicit merge/deploy authorization.
- Remaining: complete checkpoint discovery/transfer, recovery wiring, authorized unfinished-intent reconciliation, budget/live canary and rollback acceptance. 080 remains CODE_IN_PROGRESS.

### 2026-10-04 exact intermediate artifact reconciliation

- PR #166 merged `e0f7428b0a28ac4f1f020495e9615baf0225584f`; main CI `37199798087` passed; no deployment required for that Python-only PR.
- Fresh branch `codex/080-intermediate-readback-20261004` closes lost intermediate upload response using the existing authenticated/fenced upload endpoint in explicit read-only inspection mode. No write or release registration on inspection.
- Retained SQLite restart resumes with seven unique artifacts uploaded once each. Missing/corrupt reads or lease takeover retain unknown status. All-artifact STAGED verification still required.
- Local Python 848, Web 192/build/lint passed. Audit/effects: `2026-10-04-080-intermediate-readback*`. No production actions or protected original-file changes.
- Web paths trigger automatic Worker deployment and pending migration check/apply; no new migration, pending production state UNKNOWN. Stop after CI for merge/deploy-side-effect authorization.
- Remaining: cross-environment retained intent/checkpoint integration, recovery wiring and actual live canary/rollback. 080 is CODE_IN_PROGRESS.

### 2026-10-04 injected candidate publish adapter

- PR #165 merged `6eee4309b790d8807fc6fe8defbe9034a338e798`; main CI `37198596074` passed, no Worker deployment or migration.
- Fresh branch `codex/080-publish-adapter-20261004` connects verified immutable core output to ordinary runner publish through explicit injection. Shared allowance, retained intent and current lease required; all declared artifacts exact-readback verified.
- Fourteen new behavioral fixtures include actual runner invocation, no duplicate uploads on resume, upfront request ceilings and unknown-final reconciliation ordering. No CLI/workflow production registration or live acceptance.
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

## 2026-10-11 recovery control request envelope

- PR #178 merged `504e95750e42d0dfb603794d0e97775c7a83f783`; main CI 38116212581 and deploy 38116212696 succeeded. No migrations; production build identity verified.
- Existing offline task preflight now accounts for nested recovery lease/ledger requests using explicit bounded counts; unknown counts and cap/cost mismatches block.
- See `2026-10-11-080-recovery-control-preflight.md`. No production caller, workflow, migration or flag change; merge triggers main CI only.
- 080-RUNNER remains CODE_IN_PROGRESS. Next: complete runner-derived cost/lifecycle envelope and trusted entry; no live recovery until a complete authorized canary package fits resource and request bounds.

## 2026-10-11 scoped recovery checkpoint handoff

- Base PR #177 merge `ce0c56e2992aee959a8b0bcca745da468743dce0`; exact Worker deployment previously verified.
- Existing handoff protocol now binds the recovery plan/scope and approved stage prefix. No production caller, flags, migration or collection changes.
- Python 963 / Web 215 passed; lint/build passed. See `2026-10-11-080-recovery-handoff.md`, including the unchanged request-cap limitation.
- 080-RUNNER remains CODE_IN_PROGRESS. Full lifecycle/budget proof and trusted entry must precede a real canary. Stop after hosted CI for merge/deployment authorization.

## 2026-10-11 exact recovery publish runner admission

- Base PR #175 cf74f033b21b230f79400f019893a06b0f674501 deployed via 38109879189.
- Runner admits only proven recovery candidate adapter/transport with shared exact guard/task after approved publish scope binding.
- Local Python 946 / Web 212 passed, build/lint passed. No production caller or domain aliases enabled.
- Details: 2026-10-11-080-recovery-publish-runner.md. Main CI only, no auto deployment/migration; stop for merge approval after CI.
- 080-RUNNER remains CODE_IN_PROGRESS; product-domain mapping, handoff, trusted caller and real acceptance remain.

## 2026-10-11 recovery candidate staging continuation

- Base PR #174 merge d6b7e861edae3d169a98798937e96c77622737bf; deployment 38105606394 verified exact build and no migrations.
- Opt-in recovery candidate staging contract, no production caller/flag/active pointer changes.
- Local Python 939 / Web 212 passed; build/lint passed. Details: 2026-10-11-080-recovery-publication.md.
- Stop after hosted CI for merge and automatic deployment/migration-check authorization. 080-RUNNER remains CODE_IN_PROGRESS.

- PR #151 merged as `48d1e6b86931b41d50d54ff63e2d589a4b1fa177`; main CI `37084824734` succeeded.
- Fresh branch `codex/080-durable-budget-20261003` from that merge. Local-only SQLite adapter reuses existing budget transitions under bounded atomic journal replay; restart and separate processes sharing one file cannot forget allowance.
- See `2026-10-03-080-local-budget-persistence.md` for trust boundary, limits and tests. No remote shared quota store, production caller, migrations or workflow changes.
- `080-RUNNER` remains CODE_IN_PROGRESS. Cross-host/account-wide reservations, full cost evidence, checkpoint lifecycle, remaining adapters, recovery and real canary remain required.
- Stop after hosted CI for merge authorization. Original reports/latest, memory and user edits untouched.
