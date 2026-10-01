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
| `051-C` | PCF capture and qualification separation | `IN_PROGRESS` | Independent detached worktree `/Users/xiaoker/.codex/worktrees/flow-v051-c`; base `5cfd2dd8d301bf6e10daf81f6806173742ec2584`; PR pending | Not deployed | Local full Python tests, web lint/build/test passed; no PCF capture, flag change, migration, or production run | CI and review remain | Push one standalone `051-C` PR and wait for CI | Merge/deploy authorization after CI is green |
| `060-A` | Unified application checks and checkpoints | `NOT_STARTED` | — | — | — | Requires `051-C` | — | — |
| `060-B` | Unified budget and measured/estimated/unknown accounting | `NOT_STARTED` | — | — | — | Requires `060-A` | — | — |
| `060-C` | Thin workflow and local runner entry points | `NOT_STARTED` | — | — | — | Requires `060-B` | — | — |
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
- **Current isolated worktree:** `/Users/xiaoker/.codex/worktrees/flow-v051-c`.
- **Branch:** detached at `origin/main`; the PR will be pushed from `HEAD` to `codex/flow-v051-c-20261001` without changing the checkout.
- **Base:** `origin/main` at `5cfd2dd8d301bf6e10daf81f6806173742ec2584`.
- **Completed before this stage:** `051-A` and `051-B` were merged and deployed. Runtime probe and observation flags remain disabled.
- **Current in-flight work:** `051-C` separates official PCF source-window evidence from capture and qualification status, hashes the exact eligible ETF set, persists plan evidence in the existing control-plane JSON, and pauses bounded retries through the existing lease table. Local verification is complete; no PR, deployment, flag change, migration, workflow dispatch, collection, recovery, backfill, or active-release change has occurred.
- **Effective authorization:** implementation of `051-C` code and its local/CI validation only. Merge, deploy, PCF five-day observation, production flags, and data runs require a separate disclosed authorization.
- **Next allowed action:** push one standalone `051-C` PR and wait for CI; do not merge or deploy at this stop point.
- **Forbidden before the stop point:** `060-*` or later code, production runs, PCF capture, five-day observation, flag changes, migrations, active-release mutation, and destructive cleanup.
- **Protected paths:** `reports/phase0/latest-*`, `memory/`, and all other pre-existing user changes in the original worktree are untouched.
