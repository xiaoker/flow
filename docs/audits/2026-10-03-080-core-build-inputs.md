# 080-RUNNER: immutable historical build inputs

Base main: `30c167f1038e4bdaee1c79b45e59cc35893c478a` (PR #157;
main CI `37119570876` passed). Candidate code only; no live task.

## Scope

The existing `CoreReadModelBuildAdapter` now accepts optional market history and
previous manifest paths. Their explicit SHA-256 fields join the existing canonical
stage input digest. A supplied path without a declared digest, or a declared digest
without its path, is rejected before output. Old snapshot-only contracts retain
their input digest and checkpoint compatibility.

Exact sidecar bytes are saved in the isolated workspace as
`input-market-history.json` and `input-previous-manifest.json`. Resume recomputes
the existing builder with these saved inputs and verifies the full output/file set;
it never rereads the current external files to silently change historical inputs.
Existing market-history date validation and history/ETF coverage non-regression
checks remain in `build_market_read_models`; this adapter adds no alternate
normalizer or financial formula. It does not infer a previous manifest when none
was explicitly provided.

Each input and the combined snapshot+sidecar bytes must fit the existing 4 MiB
input cap. Generated outputs retain the 32 MiB/32-file cap. These are local bounds,
not provider quota or proof that a real archive fits the current checkpoint
transport. No remote limit, lifecycle, budget availability or telemetry changed.

## Tests and evidence

28 focused cases cover snapshot-only compatibility, independent optional inputs,
previous-only coverage regression refusal, byte equivalence with the
existing builder, successful historical build and saved-input resume, real runner
cross-job restore after deleting all external inputs, hash/path/JSON/date failures,
coverage regression, total input bounds and corruption of each saved sidecar.
Full local regression: Python 720 tests passed; Web 177 tests, build and lint
passed. Hosted CI awaits the standalone PR; fixture tests
are not production acceptance. No code is registered in CLI/workflows.

## Remaining entry conditions

080 is CODE_IN_PROGRESS. After merge and main CI, continue shared normalization,
verification/publication adapters and recovery composition, without creating an
alternate business implementation. Before any live canary, complete real artifact
size/whole-task budget bounds, shared reservation evidence, retention/lifecycle
evidence, lease/recovery authority and rollback package. This change does not
recover historical dates or certify long ETF periods as available.

Original worktree reports/phase0/latest-*, memory/ and other user files untouched.
Only Python/tests/docs change: PR/main CI uses Actions resources; no automatic
Worker deployment or migration. Adjacent merge-side-effects JSON records scope.
