# R0-C/R0-D controlled-recovery auditor

This is the formal, reproducible and read-only inventory entry for the
controlled recovery plan. It consumes a local JSON evidence bundle; it does
not contact Cloudflare, D1, R2, GitHub or an official market source, and it
cannot dispatch a workflow, migrate a database, publish a release, change a
flag or mutate an active pointer.

## Run contract

From the repository root, provide a bounded evidence export and the checked-in
resource budget contract:

```sh
PYTHONPATH=src:. python scripts/audit_controlled_recovery.py \
  --input /path/to/r0-evidence-bundle.json \
  --config config/resource-budget.json \
  --phase R0-C \
  --output-dir /private/tmp/flow-r0-c-audit
```

Use `--phase R0-D` only when the same formal auditor is being run for the
documented production read-only inventory. The default remains `R0-C` for
backward compatibility. Input bundles remain `schemaVersion=1`; generated
plan, budget and stage-evidence outputs use the `recovery-audit-v2` contract.
The phase label remains excluded from the canonical plan hash, while the
contract version is included, so a v1 plan hash cannot be reused as a v2
approval.

The input must be JSON with `schemaVersion=1`, a `scope` containing inclusive
ISO dates, an explicit `campaignId`, and bounded inventory rows. A row is
identified by `(dataDate, domain)` and may carry the existing raw,
normalized, model, PCF, release-slot and date-visible evidence fields. Missing
rows are expanded to `UNKNOWN`; unknown applicability or budget telemetry is
never promoted to a recovery target or a successful state.

R2 evidence is accepted only as a bounded manifest/index page with `page`,
`pageSize`, `hardLimit`, `hasMore`, `indexSha256` and optional
`suspiciousGaps`. Missing metadata, remaining pages or suspicious gaps keep
the inventory incomplete. A declared SHA mismatch is `BLOCKED`; a missing
legacy header can be represented with `computedRawSha256` and
`hashOrigin=recomputed_from_archived_bytes` without claiming the original
header was verified.

## Outputs

The isolated output directory contains exactly:

- `inventory.json` and `inventory.csv`: date × domain × stage matrix;
- `recovery-plan.json`: canonical `R0_READ_ONLY_DRAFT`, `NOT_APPROVED`,
  `executionBlocked=true`, stable `planHash` and source fingerprint;
- `source-unavailable.csv`: declared unavailable sources with a
  `provenanceStatus` indicating whether the closure evidence is complete;
- `budget-estimate.json`: measured, projected and worst-retry resource use;
- `stage-evidence.json`: machine-readable R0-C/R0-D status, reconciliation
  state and read-only assertions;
- `summary.md`: concise human review of concerns and next allowed phase.

The same fixture and contract produce the same `planHash`; current time,
workflow run IDs and download time are not part of the hash. Outputs must be
written to an isolated directory. The auditor rejects paths containing the
protected `reports/phase0` or `memory` roots before creating directories.

R0-C ends at `CODE_READY`/`DONE_WITH_CONCERNS` and can only point to
`R0-D_RECONCILE`. R0-D can emit `R0_RECONCILED` and
`nextAllowedAction=R2_ACCEPTANCE_AUDIT` only when every required evidence,
budget metric, R2 page, safety assertion and provenance predicate is closed.
Evidence-backed `SOURCE_UNAVAILABLE` is a known closed fact; an unavailable
source without provenance remains incomplete. Neither phase registers or
approves a plan, runs a migration, enables `PIPELINE_RECOVERY_ENABLED`,
dispatches Watchdog/Collector/Publish, or runs a canary. Those actions require
the next documented stop-point and an explicit authorization.
