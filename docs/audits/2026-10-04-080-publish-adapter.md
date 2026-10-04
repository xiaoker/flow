# 080: injected ordinary candidate publish stage

Base PR #165 merge: `6eee4309b790d8807fc6fe8defbe9034a338e798`.
Branch: `codex/080-publish-adapter-20261004`.

## Scope

Connects existing verified core output to the existing runner's explicitly
injected `publish` stage. Production CLI/workflow registration is unchanged.
No source requests, active pointer change, migration, remote initialization or
new service. Adapter effect admission is limited to ordinary `publish`, never
recovery or another stage. Current runner control-plane authorization and lease
guard remain mandatory; publication additionally requires SharedBudgetLedger
and an explicitly initialized retained PublicationIntentStore.

## Contract and order

Input config binds exact verify input/output hashes. Existing verify/build
validators recheck saved original input, capability/date contracts and complete
inventory. All artifacts, exact producer bytes and durable local bindings are
validated before any network effect. Immutable sources are held as bounded bytes
for uploads; no rescan can substitute changed bytes after validation.

Artifacts are uploaded in sorted order, finalizing only the last one. Existing
stage-only HTTP/lease/shared-budget contracts govern each request. Every declared
artifact is then read through the existing STAGED API and compared to its exact
hash, size, date, manifest hash and content before a local COMPLETED checkpoint.
Missing, corrupt or rejected reads cannot complete the stage.

Resume first reconciles an unknown final write by exact readback, before replay
of preceding acknowledged uploads. Already acknowledged artifacts are not
uploaded again. A local completed receipt is not fresh remote proof: checkpoint
verification rereads every artifact under the current allowance/lease guard.
Unknown intermediate uploads remain blocked, without automatic retries or an
orphan bypass. Shared budget control-plane ceilings still apply; restarting a
client does not refund server reservations or authorize writes automatically.

The retained local intent file remains outside the stage directory and is not
exported by this adapter. Therefore cross-host local-state restoration and
intermediate reconciliation are still pending. The adapter does not create/reset
that file. Callers must preserve the same bound file and execution identity.

## Tests

Twelve new fixtures cover all-artifact publication/readback, read-only resumed
completion, source/config/owner/local-budget/missing-intent rejection before
remote effects, budget denial, unknown final reconciliation without duplicate
POST, intermediate unknown block, corrupt readback, actual runner invocation and
effect rejection on a nonpublish stage.

Validation commands: full Python `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`;
Web `npm test` (build included), `npm run lint`; `git diff --check`.
Fixture authorization is not evidence of production Access or account quota.
Results: full Python 845 passed; new adapter tests 12 passed; Web 191 passed;
build, lint and diff whitespace check passed.

## Effects and next step

Only Python/tests/docs paths: PR/main CI consumes Actions minutes, no automatic
Worker deploy or migration. No production upload, collection, flags, pointer,
retention or original protected-file changes. Stop after green CI for merge.

080 remains CODE_IN_PROGRESS. Next: exact intermediate unknown-write
reconciliation and cross-environment retained intent/checkpoint integration,
then recovery wiring and a separately authorized live canary/rollback package.
No new production execution is implied by this injected adapter.
