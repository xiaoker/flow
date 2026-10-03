# 080-RUNNER: candidate offline core build adapter

Base: `42c7b3459d2416243911631f4aa47725b8a41b9f` (PR #156 merged;
main CI `37105591284` passed). This increment is code only, not deployment
or product acceptance. The original dirty worktree is untouched.

## Implemented boundary

`CoreReadModelBuildAdapter` implements the existing injected runner adapter
interface and reuses `build_market_read_models`. It accepts one exact materialized
snapshot, verifies its SHA-256 and exact `asOf == targetDate`, and supplies the
runner producer SHA and existing capability contract version. Its generated
payload/manifest bytes equal the existing pure builder output. There are no
new financial formulas, classifications or date inference rules.

The isolated stage workspace retains exact input bytes, deterministic outputs
and an atomic receipt. A completion checkpoint is reusable only after identity,
stage, input contract, receipt evidence, file set and regenerated output bytes
all agree. The runner's existing cross-job bundle can restore this workspace
without the original source file. Corruption or partial output fails closed;
partial output requires explicit reconciliation, not blind overwriting.

Local bounds: input 4 MiB, generated output 32 MiB, file count 32, receipt
64 KiB. These are candidate filesystem bounds, not evidence of provider quota
or transport capacity. Large real inputs still need measurement and the existing
bounded transport admission before any live execution. No limits were raised in
R2 transport, account admission or production.

## Verification

- Targeted tests: 14 passed, including existing-builder byte equivalence,
  exact date/hash/config rejection, artifact/receipt tampering, partial output,
  symlink/size/lease rejection, actual runner local resume and separate-job bundle
  restore after removal of the original snapshot.
- Full Python: `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`: 706 passed.
- Web: `npm test` (build + 177 tests) and `npm run lint` passed. Existing
  large client chunk warning remains; this increment changes no frontend code.
- Independent review found no demonstrated defects; its requested second
  pre-write lease-loss case is now explicitly covered.
- Fixture tests are local evidence only; no production acceptance is claimed.

## Explicit remaining work and next entry

This adapter is injectable only; CLI and workflows retain their old entrances.
This increment does not wire optional market-history / previous-manifest
sidecars, normalization, ETF-stock/PCF models, verification/publication adapters
or recovery orchestration. It cannot establish long-period coverage beyond its
snapshot, and does not claim every required capability is READY.

After merge approval and successful main CI, continue remaining shared stage
adapters and immutable sidecar input contracts from a fresh latest-main worktree.
Before canary: reconcile provider lifecycle evidence, shared budget provisioning,
complete-task upper bounds, real artifact sizes, recovery authority and rollback
package. Runtime execution and production cutover require separate authorization.
080-RUNNER remains CODE_IN_PROGRESS.

## Side effects

PR and main CI consume Actions resources (remaining account balance unknown).
Only Python, tests and audit documentation change; the current Worker deployment
path filter does not match, so merging does not deploy or apply D1 migrations.
No remote write, collection, active-pointer/flag change or retention operation.
See the adjacent merge-side-effects JSON. This report assists review; it is not
an additional approval system.
