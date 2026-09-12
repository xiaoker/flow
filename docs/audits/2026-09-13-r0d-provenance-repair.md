# R0-D legacy archive provenance repair

## Purpose and boundary

Older `historical_snapshot` rows prove that a date was visible, but do not
necessarily contain the per-source receipts that the R0-D auditor requires.
This phase closes that evidence gap without treating a visible dashboard as
proof that an exchange source was captured.

`GET /api/v1/admin/recovery-provenance` is the formal, bounded preview surface.
It reads one keyset page from `historical_snapshot`, fetches only the exact
published history manifest keys recorded by those rows, and performs bounded
R2 `HEAD` checks for the manifest's snapshot, raw object, metadata object and
normalized objects. It never lists the bucket and never downloads a raw or
normalized body. The manifest body is capped at 512 KiB.

The fixed campaign scope is `2024-08-01..2026-09-03`. A request may inspect a
smaller sub-range, but cannot leave that scope and cannot request more than
four snapshots. The preview emits:

- a stable date × domain × stage matrix;
- `readinessRows` and `pointerRows` that can repair only the existing
  `source_readiness` and `history_artifact_pointer` metadata tables;
- explicit `ARCHIVE_PROVENANCE_INCOMPLETE`, missing-object and hash issues;
- a canonical `planHash`, `recoveryPlan`, `sourceUnavailable` and resource
  counters;
- `budgetStatus: unknown`, `planStatus: NOT_APPROVED` and
  `executionBlocked: true`.

Unknown budget/account telemetry can therefore never be represented as
`DONE` or `RECOVERY_READY`. A dashboard slot, a date-visible row or a model
row is intentionally reported as `UNKNOWN` by this phase; the later R0-D
inventory remains the authority for model, dashboard and penetration stages.

## Guarded metadata apply

`POST /api/v1/admin/recovery-provenance` is disabled unless the deployment
variable `RECOVERY_PROVENANCE_REPAIR_ENABLED=1` is explicitly enabled. The
caller must use the existing Bearer `INGEST_API_KEY`, send the fixed
`x-flow-recovery-provenance-intent: recovery-provenance-apply-v1` header and
provide the exact preview `planHash`. The endpoint recomputes the bounded
preview before writing, so stale or reordered plans are rejected.
The JSON body must explicitly set `mode: "apply"` together with the same
bounded query and hash.

Apply writes only additive metadata receipts using the existing schemas:

- `history_artifact_pointer` for `etf_scale` and `etf_pcf` normalized objects;
- `source_readiness` for the existing source/dataset mappings.

Both statements use `ON CONFLICT ... WHERE excluded.observed_at >= ...` so a
newer receipt cannot be overwritten by an older archive. No `raw` facts,
read-model rows, release rows, active pointers, flags, migrations, workflow
dispatches or historical backfills are touched.

## Execution order

1. Deploy the code with the apply flag still off and run the preview page by
   page; archive the output outside the repository.
2. Review every manifest/hash/object issue and the resource counters. Missing
   or contradictory evidence stays blocked; it is not converted to
   `SOURCE_UNAVAILABLE` or filled with a zero/previous value.
3. If and only if the reviewed page is complete and the account budget is
   independently known to allow the bounded metadata writes, enable the flag
   for one controlled apply request using its exact plan hash.
4. Disable the flag, verify only the metadata rows changed, then rerun one
   formal R0-D read-only inventory. Stop at the R0-D stage report before any
   recovery, collection or publication action.

This phase is not a replacement for official source collection. It can repair
the missing provenance for objects that are still present and exactly
verifiable; a missing/invalid archive remains a real evidence gap for a later
controlled re-collection plan.
