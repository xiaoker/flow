# 080-RUNNER cross-host publication intent: implementation boundary

Status: CANDIDATE_CODE. Local two-host/concurrency fixtures are not production
acceptance. No production operation or cross-host live acceptance has run.
Base: c373e3bd440662bee76310ba168716a39003229b (PR #167).

## Verified gap

- `PublicationIntentStore` commits PENDING before candidate HTTP, but its SQLite
  file is local and outside the publish stage directory.
- Stage bundles require a COMPLETED checkpoint. They therefore cannot preserve
  a publication interrupted before completion.
- Checkpoint objects are immutable and addressed by content digest. The API
  verifies a supplied digest, not whether that object is the most recent intent.
- `SharedBudgetLedger.authorize_step` generates a new operation ID. It is a
  budget authorization, not an artifact-specific publication deduplication key.

Copying SQLite into a completed bundle, or accepting an older empty journal,
does not close the crash interval. A process may commit a local intent and send
the upload after the last remote checkpoint. A different host could then treat
the older checkpoint as absence of the upload. Budget availability does not
make that replay safe.

## Implemented bounded candidate contract

The Python candidate transport sends `operation=upload_once`. The existing
authenticated import endpoint validates exact manifest/artifact bytes and
ordinary lease, then conditionally creates a marker at
`market-data/releases/<asOf>/<releaseId>/upload-intents/<name>.json` using R2
`If-None-Match: *`. A definite successful put alone permits that invocation's
artifact effect. Owner/attempt/manifest/finalize are recorded in its bounded
4096-byte reference; they do not create different marker keys, so changing
owner or finalization cannot win another upload right for the same artifact.
The current lease is rechecked after claiming and around subsequent effects.

A different host with a newly initialized local journal may receive 409; the
local PENDING hold survives and the existing exact readback can resolve it.
This is an explicit read-only continuation, not an automatic upload retry. A
marker claimed before a crash with no artifact remains blocked. This change
does not yet authorize reclaiming such an unfinished marker or deleting it.

Compatibility: legacy upload operations remain unchanged. The cross-host
guarantee applies to the candidate transport's new upload_once contract; it is
not a retrofit proof for old clients or previously uploaded candidates.
Successful local ACK reuse still requires fresh remote STAGED verification
before a publish checkpoint can be reused.

Each candidate upload includes one extra R2 Class A operation, at most 4096
intent bytes and two additional primary-key lease reads in its conservative
cost floor. Actual account telemetry remains unknown; a cost floor is not a
measured bill. No D1 schema/migration or workflow is added. Markers share the
existing release prefix; no lifecycle/retention policy is modified. They must
not be independently removed while their candidate can be resumed.

## Remaining integration boundary

Reuse the existing candidate import authority and shared execution identity.
Before any artifact upload, persist an exact artifact-specific remote intent;
the same server operation must fence the right to execute that upload. Use an
atomic conditional transition, not SELECT followed by unconditional UPDATE.
The remote identity includes runKey, owner, attempt, targetDate, build SHA,
releaseId, exact manifest hash, artifact name/hash/size/key and finalize.

Required order:

1. Validate all producer artifacts, owner and conservative resource allowance.
2. Atomically claim the exact remote artifact intent before the upload effect.
3. Perform the bounded upload; unknown results retain the intent.
4. Verify exact stored bytes (or exact STAGED final candidate proof).
5. Persist acknowledgement; only then advance the publication checkpoint.

Cross-host resume must read the current exact remote intent under the current
lease, rather than infer permission from a copied local journal. Missing,
unknown or conflicting evidence cannot authorize a duplicate write. A takeover
must not rewrite an old owner's journal; define authorized reconciliation of
the existing immutable artifact separately from permission for a new effect.
Use the existing control plane rather than a second scheduler or state service.

The bounded R2 conditional marker supplies pre-effect deduplication without
adding unbounded JSON to pipeline_window or reusing budget operation IDs.
Recovery execution wiring, authorized handling of markers without artifacts,
full checkpoint discovery/transfer and actual runner canary remain incomplete.

## Required tests

Local validation (2026-10-04): full Python suite 850 passed; Web suite 193
passed; production build and ESLint passed. Commands:

```
PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=
cd web
npm test && npm run lint
```

The built Worker fixture runs 20 simultaneous conditional claims (one winner),
duplicate and changed-finalize requests, exact orphan inspection, crash after
claim and lease takeover during claim. The Python fixture uses two distinct
SQLite journals and a simulated remote conditional claim, then exact final
readback; it does not assert actual provider behavior or account billing.

- Two hosts, same artifact: exactly one upload right.
- Crash before HTTP, after HTTP and before local/remote acknowledgement.
- Old checkpoint/local empty journal cannot issue a second upload.
- Ambiguous claim cannot execute; exact readback resolves without upload.
- Wrong owner, expired lease, changed attempt/date/build/manifest rejected.
- Missing/corrupt remote proof remains blocked, not zero or completed.
- Existing final and intermediate exact readback behavior retained.
- Bounded D1/R2 operations and conservative shared budget, including intent IO.
- No active pointer, workflow dispatch, flags or collection side effects.

This note is not CI evidence, deployment authorization or a new operational
gate. The delivery includes client, server and crash/restart tests; it is not a
snapshot-only fix and does not mark 080-RUNNER complete.
