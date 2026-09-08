# Controlled recovery inventory exporter

This document describes PR C of the R0-D reconciliation repair.  The
exporter is a bounded, read-only adapter for the PR B
`GET /api/v1/admin/recovery-inventory` surface.  It does not collect market
data, write D1 or R2, dispatch a workflow, publish a release, register a plan,
change a feature flag, or change an active pointer.

## Fixed campaign contract

The exporter accepts only the closed campaign scope `2024-08-01..2026-09-03`
(inclusive).  The default priority dates are 2026-08-27, 2026-08-28,
2026-08-31, 2026-09-01, 2026-09-02 and 2026-09-03.  Priority dates are
evidence hints, not an assertion that a date is missing.  Applicability is
derived from the source-readiness evidence returned by the existing endpoint;
the exporter never infers a trading day from a weekday and never substitutes a
page date for an official source business date.

Pagination uses the endpoint's opaque cursor with a maximum of 250 pages and
100 dates per page.  A repeated response hash, repeated date, non-advancing
cursor, missing page, partial endpoint response, HTTP 403/429, timeout, or
response-size limit produces an explicit `INCOMPLETE` result.  No such failure
is converted to an empty `READY` inventory.

The pipeline-window and recovery lookups use a bounded lexicographic
`run_key` range instead of expanding one bound parameter per date/window. This
keeps every prepared statement below Cloudflare D1's 100-parameter limit even
when a page contains the maximum 100 dates.

## Evidence bundle

`evidence-bundle-v2.json` is the stable external artifact.  It contains the
fixed scope, page receipts (cursor, byte count, response SHA-256, next cursor
and per-page D1/R2 counters), the active release-slot snapshot, the flattened
date × domain × stage matrix, independent dashboard and ETF-penetration release
records, pipeline-window records, bounded R2 HEAD receipts, D1/resource
counters, account-telemetry status and read-only safety assertions.  URLs and
headers are never written to the artifact.

R2 evidence is limited to exact keys declared by release manifests and checked
by the PR B HEAD surface.  A date page whose manifest surface contains more
than 64 exact keys is returned as an explicit `R2_HEAD_PAGE_REMAINS` 503 with
an opaque, scope-bound `r2Cursor`; the exporter drains that cursor and merges
the artifact receipts back into one logical date page.  The endpoint performs
at most 64 HEADs per request, while one campaign stops at 2,496 HEADs (39 full
pages), preserving the documented 2,500-head ceiling.  Reaching that ceiling,
an unstable manifest/index fingerprint, or a malformed continuation remains
`INCOMPLETE` and never becomes READY.  The `r2Inventory` object records the
object hard limit, the separate `headHardLimit`, pagination status, an index
fingerprint and suspicious gaps.  It is not a bucket listing and it never
downloads object bodies.

The exporter accepts HTTP 503 only when the response body is exactly
`INCOMPLETE` with `R2_HEAD_PAGE_REMAINS` and a valid advancing `r2Cursor`.
Generic 503 responses, partial HEAD pages, and pages with a non-full
continuation size fail closed.  The cursor contract is bounded to 256 encoded
characters and includes the original date scope, so a cursor cannot be reused
for another campaign.

R0-D has two deliberately separate budget evidence layers.  Its hard closure
gate is the finite task-local envelope in `config/resource-budget.json`:
500,000 D1 rows read, zero D1 rows written, 300 Worker requests, zero R2 Class-A
operations, 2,496 R2 Class-B operations, 250 logical inventory pages, 64 MiB of
responses and a 20-minute workflow timeout.  The exporter records exact local
counters; an unknown counter, disagreement with the independent D1 audit, a
write operation or an exceeded cap fails closed.  A successful report labels
this basis `LOCAL_BOUNDED`.

An optional current, hashed official account-usage export may still be supplied
with `--usage-file`.  It must contain `capturedAt`, `metrics` and a SHA-256 over
the unsigned payload and must be no more than 60 minutes old (or before its
`expiresAt`).  When valid and within budget, the report labels the combined
evidence `ACCOUNT_VERIFIED`.  Missing, expired, malformed or mismatched account
telemetry remains explicit advisory evidence; it no longer prevents this
read-only inventory from becoming `R0_RECONCILED` when every local counter and
all data-integrity predicates are proven.

This exception is scoped only to R0-D reconciliation.  It does not authorize a
recovery, backfill, collection or publication.  Those mutation-capable stages
continue to use their separate account-level budget gate and must fail closed
when required provider usage is unknown or over budget.

The manual workflow may receive the optional export through the repository
secret `FLOW_OFFICIAL_USAGE_EXPORT_JSON`.  When present, it writes the value
only to an ephemeral runner path outside the uploaded artifact, passes that
path to `--usage-file`, and removes it in an `always()` cleanup step.  When the
secret is absent, no file is created and the exporter records account telemetry
as `UNKNOWN`; the bounded read-only run proceeds without requiring a user to
manufacture temporary provider data.

## Offline audit and safety boundary

The existing `trading_desk.recovery_audit` remains the sole owner of status,
stage and plan semantics.  The exporter converts the v2 bundle to its
established v1 input contract, runs the auditor twice, and requires identical
`planHash` values.  The generated plan remains `NOT_APPROVED` with
`executionBlocked=true`; this PR does not approve or register it.

The output directory must be isolated (`.recovery/` or a temporary directory).
The writer rejects paths containing `reports/phase0` or `memory`.  The
workflow uploads artifacts for 14 days and does not commit them to the
repository.

## Manual workflow

`.github/workflows/controlled-recovery-inventory.yml` has only a
`workflow_dispatch` entry, `contents: read` permissions and one fixed
concurrency group.  It performs scope/credential preflight before invoking the
stdlib-only exporter and uploads an artifact even when a preflight, pagination
or budget check fails.  Its `timeout-minutes` value is tested against the
checked-in task-local envelope.  It has no schedule, no workflow-dispatch
capability, and no collection, publish, migration, recovery or canary step.

Running this workflow is the later R0-D-RUN action.  It must not be run as part
of PR C implementation or CI.  After a real run, a stage report must be
reviewed and the process must stop unless every R0-D closure predicate is
proven.
