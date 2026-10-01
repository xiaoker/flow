# Flow convergence progress

This is a human-readable handoff index for the roadmap at
`docs/handoff/2026-09-23-flow-target-architecture-and-convergence-roadmap.md`.
It is not a production gate, database protocol, or replacement for GitHub,
R2, D1, release, or receipt evidence.

## Current stage

| Stage ID | Scope | Code status | PR / HEAD / merge SHA | Deployment | Runtime / data evidence | Blocker | Next action | Authorization needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `051-A` | Non-interfering source observation error boundary | `MERGED_DEPLOYED` | PR #130; merge `1894f0588efb4665f847ba4aebd2f6401164de9e`; normalized-evidence ordering follow-up PR #137, merge `6d7c5a39bc1731ac32236200aefd53bcbee86e28` | PR #137 deploy run `36874232795` succeeded; Worker build identity verified | Receipt creation follows normalized evidence verification; source fingerprint v0.5.1 includes normalized identity; flags unchanged | None for this item | Keep sidecar failures non-fatal only for classified observation storage errors | — |
| `051-B` | Probe OFF/OBSERVE/ENFORCE contract and date coordination | `MERGED_DEPLOYED` | PR #131; merge `5cfd2dd8d301bf6e10daf81f6806173742ec2584`; main CI `36853968085` succeeded | Deploy `36853967739` succeeded; production identity verified | `MARKET_DAY_PROBE_ENABLED=0`, no runtime observation or collection dispatched; no migration pending | None for this item | Implement `051-C` code only; keep PCF read flag off | Merge/deploy authorization after CI is green |
| `051-C` | PCF capture and qualification separation | `MERGED_DEPLOYED` | PR #132; merge `f1d1741947535b8589fa762dfb91939a6920f6d2` | Deploy run `36858489158` attempt 2 succeeded; main CI `36858490693` succeeded; Worker version `f4f7fab0-ea72-44e2-b353-ebede8beaa33` | Runtime flags unchanged (`SOURCE_OBSERVATION_SHADOW_ENABLED=0`, `MARKET_DAY_PROBE_ENABLED=0`, `PCF_CAPTURE_V2_READ_ENABLED=0`); no migration pending; no collection/PCF run | None for this item | Implement `060-A`; keep new executor metadata additive and production entry unchanged | — |
| `060-A` | Unified application checks and checkpoints | `MERGED` | PR #133; squash merge `07c06ce8278ed27895f9334319e2bd71fb472042` | No deployment required; no workflow or production entry change | Shared identity/plan/checkpoint/retry seam is on `origin/main`; legacy collect/publish/recovery wrappers preserved; CI passed | None | Implement `060-B`; keep budget admission candidate-only | — |
| `060-B` | Unified budget and measured/estimated/unknown accounting | `MERGED` | PR #134; squash merge `8258d779b5ca7affce6895e4bdb729bf871c7f70` | No deployment required; no workflow or production entry change | Unknown-aware vectors, conservative admission, atomic reference ledger and stage-report basis changes are on `origin/main`; CI passed | None | Implement `060-C`; keep candidate entry disabled for production | — |
| `060-C` | Thin workflow and local runner entry points | `MERGED` | PR #135 merge `d21d70c826d4faf02c3fa887244bc3babe2edefb`; PR #136 merge `f01678e5eb68a06cf57f73a3d3864bc371905be1` | No Worker deployment required; candidate workflow remains manual-only | Candidate CLI/runner validates exact SHA, dependency lock, auth, lease and recovery envelope; candidate entry remains outside production schedule/read path | None for this code item | Continue with `070-A`; do not use the manual candidate entry for production without its own authorized canary | — |
| `070-A` | Capability contracts and immutable candidate manifest | `IN_PROGRESS` | branch `codex/070a-capability-contract-20261001`; worktree `/private/tmp/flow-070a-capability-contract-20261001`; base `6d7c5a39bc1731ac32236200aefd53bcbee86e28` | Not deployed; PR changes Worker code and merge will trigger the deploy workflow | Schema-v4 capability contract is opt-in; core can be READY while PCF/search/detail remain unavailable; v4 is stage-only and activation rejects it; old v2/v3 contract remains compatible | Complete full tests, one standalone PR and CI | After merge and Worker deploy identity verification, proceed to `070-B`; no production candidate upload or read-path switch | Merge/deploy authorization after CI and one-time side-effect review |
| `070-B` | Period dependency and historical repair planner | `NOT_STARTED` | — | — | — | Requires `070-A` | — | — |
| `070-C` | Candidate UI/API and period coverage states | `NOT_STARTED` | — | — | — | Requires `070-B` | — | — |
| `OBS-COORD` / `OBS-PCF` / `OBS-CORE` | Independent real-day observation lines | `NOT_STARTED` | — | — | — | Each has its own code and deployment prerequisites | Submit an explicit bounded execution package when eligible | Separate runtime authorization |
| `080-*` | Production runner/coordinator/core/PCF cutovers | `NOT_STARTED` | — | — | — | Candidate evidence and explicit cutover authorization | One bounded canary and ten-day acceptance per capability | Separate production authorization |
| `090-*` | Bounded history repair and compatibility retirement | `NOT_STARTED` | — | — | — | Cutover evidence | Plan and execute one bounded batch at a time | Separate recovery/cleanup authorization |
| `100-ACCEPT` | Stable operations and final product sign-off | `NOT_STARTED` | — | — | — | All relevant SLO evidence | Twenty consecutive real business days | Final sign-off |

## Current handoff block

- **Last checked:** 2026-10-01; the roadmap revision in the user-provided handoff is the controlling implementation order.
- **Original repository:** `/Users/xiaoker/Documents/交易台` (dirty user worktree; read-only for this task).
- **Current isolated worktree:** `/private/tmp/flow-070a-capability-contract-20261001`.
- **Branch:** `codex/070a-capability-contract-20261001`.
- **Base:** `origin/main` at `6d7c5a39bc1731ac32236200aefd53bcbee86e28`.
- **Completed before this stage:** `051-A` through `051-C` merged; `060-A` through `060-C` merged (PR #135 and #136 complete `060-C`); PR #137 corrected source-observation receipt ordering and deployed. Observation/probe and PCF read flags remain unchanged/off.
- **Current in-flight work:** `070-A` adds an opt-in schema-v4 capability contract to the existing manifest, proves slot ownership and exact immutable artifact references, preserves v2/v3 compatibility, and hard-blocks v4 activation. No migration, workflow dispatch, collection, candidate upload or production read-path change is included.
- **Effective authorization:** continue roadmap code implementation, update the progress index, push/create the 070-A PR and run the necessary CI within the existing budget. Merge/automatic Worker deployment, production candidate upload, flags, observations and data runs still require the stage-specific authorization described in the PR side-effect report.
- **Next allowed action:** finish 070-A tests/docs and side-effect report, then push one standalone PR and wait for CI. After merge and deployment verification, fetch latest `main` and start 070-B in a new worktree.
- **Forbidden before 070-A merge/deploy:** 070-B or later code, production candidate upload/activation, production read-path switch, feature flag changes, migrations, collection, recovery, history repair, active-release mutation and destructive cleanup.
- **Protected paths:** `reports/phase0/latest-*`, `memory/`, and all other pre-existing user changes in the original worktree are untouched.
