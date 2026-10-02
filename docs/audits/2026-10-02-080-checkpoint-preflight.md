# 080-RUNNER offline checkpoint preflight

Base: `979115dfb20c52fdf3ad5941068a03c07d0d6fa0` (merged/deployed PR #148).

## Scope and semantics

`trading_desk.pipeline_checkpoint_preflight.checkpoint_preflight` is a pure,
in-process diagnostic. It validates exact immutable bundle identity and bytes
using the existing verifier, then evaluates caller-supplied account usage,
reservations and complete task upper bounds with the existing resource budget
contract. It neither retrieves nor manufactures official telemetry.

It reports encoded bytes, decoded bytes, file count and the current 1 MiB
transport limit. Oversize bundles are blocked; no automatic splitting, raised
Worker cap, source recapture or partial output upload is introduced.

The transport cost floors cover a POST and its server readback, plus one possible
ambiguity GET. They are **not** a complete task estimate. Lease reads/renewals,
all other stages, execution minutes and account reservations must be included in
the task estimate by the caller. A numeric zero below a transport floor is
rejected; missing metrics, limits or stale measurements retain UNKNOWN.

`LOCAL_INPUTS_CHECKED` means only these supplied local inputs passed. Every
report has `liveExecutionAllowed=false`. Remaining requirements explicitly
include verified checkpoint lifecycle, atomic shared resource reservation,
complete task cost proof and runtime authorization. The existing in-memory
reference budget ledger is not misrepresented as cross-job production locking.

## Current factual gaps

- `config/resource-budget.json` does not define R2 Class A/B caps. Evaluating
  that unchanged configuration therefore cannot return an admissible budget.
  Tests use explicit fixture caps, not purported provider limits.
- Checkpoint-object expiration/retention has not been installed or verified.
- Actual production core bundle sizes and account remaining quota are UNKNOWN.
- No new runner entry, CLI, workflow or production path is wired to this helper.
- This is not runtime budget enforcement and does not complete 080-RUNNER.

## Verification and review

Twelve new tests cover deterministic exact sizing, no filesystem effects,
unknown/stale/exhausted/missing-limit budget evidence, zero-cost understatement,
oversize diagnosis, corrupt bundle rejection and required finite freshness TTL.
Full Python suite: 586 passed. Web `npm test` (includes build): 166 passed;
`npm run lint` and `git diff --check` passed. Hosted results are linked in the PR.
Locked dependency audit still reports 24 existing vulnerabilities; this PR does
not alter dependencies or claim that finding is resolved.

Manual review against `docs/code-review-checklist.md`: date/hash contracts reused;
no formula/classification/auth changes; no provider calls, credential inputs,
pointer mutation or deletion. Fixtures do not claim production verification.

## Merge effects and next step

PR/main CI consumes Actions minutes. Only `src/**`, `tests/**`, `docs/**` change,
so the existing Worker deployment path filter does not match. No SQL, migration,
production flag or data operation is introduced. Revert this isolated commit
to roll back; no stored object/schema compatibility changes occur.

After CI, stop for explicit merge authorization. Then continue candidate-only
lifecycle and shared resource integration using existing mechanisms, followed
by remaining adapters and a bounded, separately authorized runtime package.
