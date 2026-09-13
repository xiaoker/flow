# R0-D bounded provenance preview workflow

## Purpose

The deployed `GET /api/v1/admin/recovery-provenance` endpoint is the formal
read-only surface for checking legacy archive provenance.  This change adds a
small, independent client and a manual GitHub Actions workflow so the surface
can be replayed with the same fixed campaign contract and attached as evidence.

It is not a recovery runner and it does not replace the R0-D auditor.  A
preview result remains `NOT_APPROVED`, `PENDING` and `executionBlocked=true`.
It cannot register or approve a plan, write D1/R2, dispatch a workflow, publish
a release, change an active pointer or change a feature flag.

## Fixed bounds

- Scope is exactly `2024-08-01..2026-09-03`.
- API page size is fixed at four snapshots and the cursor must advance.
- At most 250 pages and 64 MiB of response bytes are accepted.
- The client only sends authenticated `GET` requests to
  `/api/v1/admin/recovery-provenance`.
- HTTP errors, malformed pages, secret-shaped response fields, non-advancing
  cursors, and contract mismatches fail closed; they are never turned into an
  empty or ready result.
- Output is written only below an isolated temporary directory.  Protected
  `reports/phase0` and `memory` paths are rejected.

## Workflow use

Run **Recovery provenance preview (read-only)** manually with the fixed
defaults.  The workflow requires the existing `FLOW_INGEST_API_KEY` and,
when the site is protected by Cloudflare Access, the existing Access client
credential pair.  It does not require `FLOW_OFFICIAL_USAGE_EXPORT_JSON`, since
account billing telemetry is a separate advisory input and is not provenance
evidence.

The uploaded seven-day artifact contains one canonical JSON file per API page,
`provenance-preview.json`, and a safe `failure.json` when transport or
contract validation fails.  Authorization headers and provider response URLs
are never written to those files.  A page can be `INCOMPLETE` while still
being valid evidence; only a transport/contract error makes the workflow fail.

## Local replay

```sh
PYTHONPATH=src:. FLOW_INGEST_API_KEY='(existing key)' \
  python scripts/preview_recovery_provenance.py \
    --base-url https://flow.xiaoker.com \
    --start 2024-08-01 \
    --end 2026-09-03 \
    --limit 4 \
    --output-dir /private/tmp/flow-r0d-provenance-preview
```

The command is an evidence preview only.  Review its page statuses, issue
codes, plan hashes and resource counters; do not treat a ready metadata plan
as permission to call the guarded `POST` repair endpoint.  R0-D remains the
sole owner of reconciliation status and the process must stop at its next
documented confirmation point.
