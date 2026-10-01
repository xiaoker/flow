# Flow convergence progress

This is a human-readable handoff index for the roadmap at
`docs/handoff/2026-09-23-flow-target-architecture-and-convergence-roadmap.md`.
It is not a production gate, database protocol, or replacement for GitHub,
R2, D1, release, or receipt evidence.

## Current stage

| Stage ID | Scope | Code status | PR / HEAD / merge SHA | Deployment | Runtime / data evidence | Blocker | Next action | Authorization needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `051-A` | Non-interfering source observation error boundary | `MERGED_DEPLOYED` | PR #130; merge `1894f0588efb4665f847ba4aebd2f6401164de9e` | Deploy run `36839986466` succeeded; main CI `36839986878` succeeded | Tree identity and remote migration/deploy checks passed; no production flag change | None for this item | Keep sidecar failures non-fatal only for classified observation storage errors | — |
| `051-B` | Probe OFF/OBSERVE/ENFORCE contract and date coordination | `MERGED_DEPLOYED` | PR #131; merge `5cfd2dd8d301bf6e10daf81f6806173742ec2584`; main CI `36853968085` succeeded | Deploy `36853967739` succeeded; production identity verified | `MARKET_DAY_PROBE_ENABLED=0`, no runtime observation or collection dispatched; no migration pending | None for this item | Implement `051-C` code only; keep PCF read flag off | Merge/deploy authorization after CI is green |
| `051-C` | PCF capture and qualification separation | `MERGED_DEPLOYED` | PR #132; merge `f1d1741947535b8589fa762dfb91939a6920f6d2` | Deploy run `36858489158` attempt 2 succeeded; main CI `36858490693` succeeded; Worker version `f4f7fab0-ea72-44e2-b353-ebede8beaa33` | Runtime flags unchanged (`SOURCE_OBSERVATION_SHADOW_ENABLED=0`, `MARKET_DAY_PROBE_ENABLED=0`, `PCF_CAPTURE_V2_READ_ENABLED=0`); no migration pending; no collection/PCF run | None for this item | Implement `060-A`; keep new executor metadata additive and production entry unchanged | — |
| `060-A` | Unified application checks and checkpoints | `MERGED` | PR #133; squash merge `07c06ce8278ed27895f9334319e2bd71fb472042` | No deployment required; no workflow or production entry change | Shared identity/plan/checkpoint/retry seam is on `origin/main`; legacy collect/publish/recovery wrappers preserved; CI passed | None | Implement `060-B`; keep budget admission candidate-only | — |
| `060-B` | Unified budget and measured/estimated/unknown accounting | `MERGED` | PR #134; squash merge `8258d779b5ca7affce6895e4bdb729bf871c7f70` | No deployment required; no workflow or production entry change | Unknown-aware vectors, conservative admission, atomic reference ledger and stage-report basis changes are on `origin/main`; CI passed | None | Implement `060-C`; keep candidate entry disabled for production | — |
| `060-C` | Thin workflow and local runner entry points | `IN_PROGRESS` | branch `codex/flow-v060-c-20261001`; worktree `/private/tmp/flow-v060-c-20261001`; base `8258d77` | Not deployed; candidate workflow is manual-only | Shared candidate CLI/runner validates exact SHA, dependency lock, auth, lease and recovery envelope; local/Linux fixture paths are being verified | Local/CI verification and PR review remain | Finish tests/docs, push and create one PR; stop before `070-A` | Merge and any required deployment authorization after CI |
| `070-A` | Capability contracts and immutable candidate manifest | `NOT_STARTED` | — | — | — | Requires `060-C` | — | — |
| `070-B` | Period dependency and historical repair planner | `NOT_STARTED` | — | — | — | Requires `070-A` | — | — |
| `070-C` | Candidate UI/API and period coverage states | `NOT_STARTED` | — | — | — | Requires `070-B` | — | — |
| `OBS-COORD` / `OBS-PCF` / `OBS-CORE` | Independent real-day observation lines | `NOT_STARTED` | — | — | — | Each has its own code and deployment prerequisites | Submit an explicit bounded execution package when eligible | Separate runtime authorization |
| `080-*` | Production runner/coordinator/core/PCF cutovers | `NOT_STARTED` | — | — | — | Candidate evidence and explicit cutover authorization | One bounded canary and ten-day acceptance per capability | Separate production authorization |
| `090-*` | Bounded history repair and compatibility retirement | `NOT_STARTED` | — | — | — | Cutover evidence | Plan and execute one bounded batch at a time | Separate recovery/cleanup authorization |
| `100-ACCEPT` | Stable operations and final product sign-off | `NOT_STARTED` | — | — | — | All relevant SLO evidence | Twenty consecutive real business days | Final sign-off |

## Current handoff block

- **Last checked:** 2026-10-01; the roadmap revision in the user-provided handoff is the controlling implementation order.
- **Original repository:** `/Users/xiaoker/Documents/交易台` (dirty user worktree; read-only for this task).
- **Current isolated worktree:** `/private/tmp/flow-v060-c-20261001`.
- **Branch:** `codex/flow-v060-c-20261001`.
- **Base:** `origin/main` at `8258d779b5ca7affce6895e4bdb729bf871c7f70`.
- **Completed before this stage:** `051-A`, `051-B`, `051-C` were merged and deployed; `060-A` is merged as PR #133; `060-B` is merged as PR #134. Runtime observation/probe and PCF read flags remain disabled.
- **Current in-flight work:** `060-C` adds one shared candidate pipeline entry for local and manual Linux fixture execution. It validates exact producer SHA, dependency lock, existing auth/recovery approval, lease freshness, checkpoint resume and no active-release mutation. Existing production workflows and production entry points remain unchanged.
- **Effective authorization:** implementation of `060-C` code, tests, documentation, push and one CI run. Merge, deployment, runner cutover, observation, production flags and data runs require a separate disclosed authorization.
- **Next allowed action:** finish 060-C verification, push and create its standalone PR; stop after CI until merge/deployment authorization.
- **Forbidden before the stop point:** `070-A` or later code, production runs, budget cutover, new production entry, flag changes, migrations, active-release mutation and destructive cleanup.
- **Protected paths:** `reports/phase0/latest-*`, `memory/`, and all other pre-existing user changes in the original worktree are untouched.
