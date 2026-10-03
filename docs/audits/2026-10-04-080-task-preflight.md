# 080-RUNNER: offline whole-task preflight

Base: `bd2749a76a3ead70f4242c74f6b4a30f3e65eef7` (PR #159); main CI `37137817176` passed.

The pure `pipeline_task_preflight` entry reuses application plans, immutable bundle verification, resource vectors and checkpoint lifecycle diagnostics. It sums every declared stage plus explicit lease/control, checkpoint I/O and platform runtime estimates. Missing components remain UNKNOWN. Up to six existing application stages and existing per-bundle limits bound verification; there are no provider calls or writes.

Every bundle must match its stage, input hash and application identity. Actual encoded/decoded sizes and aggregate transport operation floors are reported. Checkpoint floors are compared to the checkpoint component, so costs attributed to another stage cannot conceal an underestimated checkpoint allowance. The aggregate task, rather than one stage, is evaluated against account usage and outstanding reservations.

`LOCAL_INPUTS_CHECKED` is not execution authority. Caller estimates do not prove retry/lease cost completeness or authenticated provider usage. Lifecycle diagnostics do not prove a deployed policy. `liveExecutionAllowed` and `resourceReservationPerformed` are always false. Account quotas, real artifact costs and production acceptance remain UNKNOWN here.

Tests cover stable canonical plan hashes, pure repeated evaluation, missing/unknown/stale evidence, aggregate exhaustion, understated transport, corrupt bundles, transport caps and malformed contracts. Full local/hosted results are recorded in the PR. No fixture is production evidence.

Manual review uses `docs/code-review-checklist.md`, especially sections 3, 4 and 6. No dates, formulas, classification, authentication, production entry/reads, workflows, migrations, flags, retention or pointer semantics change. Original protected files are untouched. Rollback is code revert; no data rollback is required.

080 remains CODE_IN_PROGRESS. Remaining ordered work includes remaining candidate publication/recovery adapters, runtime integration with authenticated shared reservation and complete bounded cost evidence, lifecycle acceptance, then a separately authorized canary/rollback exercise. This helper does not bypass those requirements. Stop after CI for merge authorization.
