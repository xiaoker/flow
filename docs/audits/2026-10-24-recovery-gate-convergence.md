# Recovery gate convergence (2026-10-24)

This change reduces manual evidence assembly for a single archived Phase 0
recovery. It does **not** approve a recovery, reconcile an account, activate a
release, or relax date/hash/QA checks.

## Gate decisions

| Gate | Treatment | Reason |
| --- | --- | --- |
| Official business date, exact source bytes, lineage and QA | Keep | Incorrect financial data must not become a published fact. |
| runKey, lease, idempotent operation identity and active-pointer CAS | Keep | Prevent duplicate work and partial publication. |
| Account-wide free-tier admission and UNKNOWN holds | Keep | A stale account reading is not proof of unused quota; an expired hold is not zero usage. |
| Hand-assembled archive object list | Replace with deterministic builder and exact read-back | It is a mechanical translation of immutable collector bytes and stored key rules, not a policy decision. |
| Separate approval for each already-authorized substep | Collapse at orchestration boundary only | One bounded recovery plan should cover its internal steps; distinct production runs or a wider scope still need authority. |
| Internal HTTP request ceiling | Derive from actual full-task graph; do not treat it as platform quota | A local cap can be revised with measured failure envelope without raising the Cloudflare/GitHub free-tier cap. |

## Archive input builder

`scripts/build_phase0_archive_recipe.py` reads the immutable collector
`history-bundle.json`, its Phase 0 manifest, and a separately proven official
calendar. It reconstructs the exact raw, metadata and normalized R2 keys and
hashes using the current history-import write contract. An adjacent-day ETF
object is read by its existing exact key and SHA, never inferred from a latest
pointer. The builder calls the existing `validate_recipe` before writing its
output. It neither lists a bucket nor fetches official websites.

Example, for an isolated **read-only** preparation session:

```bash
PYTHONPATH=src:. .venv/bin/python scripts/build_phase0_archive_recipe.py \
  --manifest /path/to/latest-manifest.json \
  --history-bundle /path/to/history-bundle.json \
  --official-calendar /path/to/verified-calendar.json \
  --target-date YYYY-MM-DD \
  --verify-live-archive \
  --output /private/tmp/archive-recipe-candidate.json
```

`--verify-live-archive` requires the existing `FLOW_INGEST_API_KEY` and
Cloudflare Access client credentials in the environment. It performs at most
one exact authenticated GET per approved object, including any adjacent
object, with no retry or redirect. The endpoint and origin are fixed.
It checks returned identity, size, SHA and bytes; the output records the exact
read count. Without this option and with a local adjacent file, output is
explicitly `UNVERIFIED_CANDIDATE`, not execution permission. Neither mode
registers a plan or writes D1/R2.

The recipe is still subject to the plan hash, recovery approval, live budget
admission and complete QA. The current 2026-10-22 case must not execute merely
because this generator succeeds: current account evidence and the old UNKNOWN
hold must be reconciled through the existing formal budget protocol, and the
verified recipe must be bound to one fixed, bounded recovery plan.

## Next bottleneck, separate from this code change

The shared budget state was last observed with evidence from 2026-10-17. The
one-hour freshness rule is a genuine account-wide admission check, not a
reason to invent remaining quota. The next improvement should automate
collection of provider-backed, period-correct evidence and show per-dimension
reconciliation diagnostics through the existing budget endpoint. It must
retain all UNKNOWN reservations, use existing CAS/operation IDs, and keep
read-only/local work available while write admission is unavailable. Do not
replace this with a manually fabricated JSON or a higher internal limit.
