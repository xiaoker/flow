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
by the PR B HEAD surface.  The `r2Inventory` object records a hard limit,
pagination status, an index fingerprint and suspicious gaps.  It is not a
bucket listing and it never downloads object bodies.

Account usage is accepted only from a current, hashed official export supplied
with `--usage-file`.  The file must contain `capturedAt`, `metrics` and a
SHA-256 over the unsigned payload and must be no more than 60 minutes old (or
before its `expiresAt`).  Missing, expired, malformed or mismatched usage is
retained as `unknown`; it cannot produce `R0_RECONCILED`.  A future provider
usage API adapter may feed the same contract without changing the auditor.

The manual workflow receives this export through the repository secret
`FLOW_OFFICIAL_USAGE_EXPORT_JSON`.  It validates the contract before making
the production inventory request, writes the value only to an ephemeral runner
path outside the uploaded artifact, passes that path to `--usage-file`, and
removes it in an `always()` cleanup step.  Because the export expires after 60
minutes, it must be generated from current official Cloudflare/GitHub usage
telemetry immediately before the run; it is not a durable baseline or a
replacement for provider APIs.

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
or budget check fails.  It has no schedule, no workflow-dispatch capability,
and no collection, publish, migration, recovery or canary step.

Running this workflow is the later R0-D-RUN action.  It must not be run as part
of PR C implementation or CI.  After a real run, a stage report must be
reviewed and the process must stop unless every R0-D closure predicate is
proven.
