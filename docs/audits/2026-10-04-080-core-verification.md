# 080 candidate core verification adapter

Base: `aa275167ed2b2ed5ee7461c374be6cc1523eab41` (PR #158).
Main CI `37123306214` succeeded before this worktree was created.

## Scope and trust boundary

`CoreReadModelVerifyAdapter` implements the existing runner's `verify` seam.
It binds exact build input and output digests and uses
`CoreReadModelBuildAdapter.verify_checkpoint` to regenerate and verify the
existing core builder output, saved snapshot and optional history sidecars.
It does not implement another capability/period/date validator or a publisher.

The core builder emits seven dashboard artifacts, not all penetration shards.
The adapter must therefore not call the full-source slot splitter or require PCF.
Confirmed publication status is additionally required. This is local candidate
verification, **not** a substitute for raw lineage/release QA, remote capability
checks, runtime budget admission, upload/activation or visible-data verification.

Saved verified build bytes and the stage receipt are isolated and bounded:
existing 4 MiB build inputs, 32 MiB output ceiling, 64 KiB build receipt,
32 source files maximum. These are local safety limits, not provider quotas.
The saved copy is reverified before writing a completion receipt. Partial
workspaces require reconciliation; lease loss cannot produce completion.
Resume uses saved exact bytes, including cross-job bundle restore under the
existing runner lease/identity checks. No CLI/workflow registration changed.

## Tests and review

- Full Python: 740 passed (`PYTHONPATH=src:. .../.venv/bin/python -m pytest -q -o addopts=`).
- Web: `npm test` (production build and 177 tests), `npm run lint`: passed.
- New verification tests: 20, covering exact identity/digest, corruption,
  pending/blocked/null publication, missing files, links, resource bounds,
  partial output, lease loss before/after copying and separate-job restore.
- Existing slot splitter is unchanged; core and PCF independence is preserved.
- Manual review against `docs/code-review-checklist.md`: no SQL, authentication,
  date/formula changes or remote operations. The existing large-chunk build
  warning remains unrelated to this Python candidate increment.
- Hosted CI awaits the standalone PR; no production acceptance is claimed.

## Effects, stop and next

Only Python, tests and these audit/index files change. No Worker deployment,
migration, source requests, R2/D1 writes, active pointer, feature flags,
retention, collection or protected original-worktree changes. CI consumes
Actions minutes; official account availability is UNKNOWN and no live task
is admitted using local test evidence.

Stop at green PR CI for merge authorization. After merge and main CI, continue
remaining normalization/publication/recovery integration and complete full-task
resource/lifecycle evidence before proposing a separately authorized real canary.
080 remains CODE_IN_PROGRESS; this PR does not make the executor production-ready.
