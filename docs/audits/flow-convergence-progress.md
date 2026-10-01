# Flow convergence progress

This is a human-readable handoff index for the roadmap at
`docs/handoff/2026-09-23-flow-target-architecture-and-convergence-roadmap.md`.
It is not a production gate, database protocol, or replacement for GitHub,
R2, D1, release, or receipt evidence.

## Current stage

| Stage ID | Scope | Code status | PR / HEAD / merge SHA | Deployment | Runtime / data evidence | Blocker | Next action | Authorization needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `051-A` | Non-interfering source observation error boundary | `CODE_READY` | Branch `codex/flow-v051-a-20260923`, base `11266dc11d258433a8e9c4d7ef8e2baf58e05303`; PR not created yet | Not deployed | Local web/Python tests and lint passed; no production run started | CI and review remain | Push one standalone `051-A` PR and wait for CI | Merge/deploy authorization after CI is green |
| `051-B` | Probe OFF/OBSERVE/ENFORCE contract and date coordination | `NOT_STARTED` | — | — | — | Requires merged and deployed `051-A` | Start a new worktree from the merged `origin/main` | — |
| `051-C` | PCF capture and qualification separation | `NOT_STARTED` | — | — | — | Requires merged and deployed `051-B` | Start only after `051-B` is verified | — |
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

- **Last checked:** 2026-09-23; the roadmap revision in the user-provided handoff is the controlling implementation order.
- **Original repository:** `/Users/xiaoker/Documents/交易台` (dirty user worktree; read-only for this task).
- **Current isolated worktree:** `/Users/xiaoker/.codex/worktrees/flow-v051-a-20260923/交易台`.
- **Branch:** `codex/flow-v051-a-20260923`.
- **Base:** `origin/main` at `11266dc11d258433a8e9c4d7ef8e2baf58e05303` when this stage started.
- **Completed in this stage:** the history-import route now treats only classified R2 observation-sidecar failures as degraded observation; D1 projection unavailability is reported without failing the primary import. Raw/hash/auth/date/QA and receipt identity conflicts remain hard failures.
- **Current in-flight work:** local verification is complete; no PR, deployment, flag change, migration, workflow dispatch, collection, recovery, backfill, or active-release change has occurred.
- **Effective authorization:** implementation of `051-A` code and its local/CI validation only. Merge, deploy, production flags, real observations, and data runs require a separate disclosed authorization.
- **Next allowed action:** push one standalone `051-A` PR and wait for CI; do not merge or deploy at this stop point.
- **Forbidden before the stop point:** `051-B` or later code, production runs, source observation shadow execution, recovery/backfill, PCF capture, flag changes, migrations, active-release mutation, and destructive cleanup.
- **Protected paths:** `reports/phase0/latest-*`, `memory/`, and all other pre-existing user changes in the original worktree are untouched.
