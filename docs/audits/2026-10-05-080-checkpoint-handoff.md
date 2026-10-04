# 080-RUNNER: exact immutable checkpoint handoff

Base: `814b0be96240b7497f6aa6e05be10e0dab8da917` (PR #168).
Candidate code only; no production execution, collection or cutover.

## Contract and integration

`run_pipeline_request` accepts explicitly injected `export_handoff=True` only
with durable bundle export, and `resume_handoff_sha256` only with an existing
checkpoint transport and no competing resume inputs. These are not CLI inputs
or new scheduled production callers.

The producer uploads and verifies each completed stage bundle first. It then
publishes a bounded immutable handoff through the existing checkpoint endpoint
using the reserved `handoff` namespace and the digest of the full plan. The
handoff includes exact identity/build/date, dependency lock, ordered stages,
all stage input hashes and only a nonempty confirmed prefix of remote bundle
descriptors. Each descriptor has exact key/hash/size. No source bytes, secrets,
local file paths, SQLite databases or authorization assertions are included.

After confirmed upload, the producer atomically stores its locator at isolated
`state_dir/handoff.json`; the final report also returns `checkpointHandoff`.
A later-stage crash leaves the previous confirmed prefix available. An
unconfirmed new handoff does not advance the locator.

The recipient supplies this **trusted exact hash**, downloads one exact object,
validates its plan and references, and passes the verified references to the
existing bundle restore path. Bundle actual byte count, hash, checkpoint,
runner binding and adapter output are rechecked before completion is reused.
Current lease and resource authorization apply to every remote operation.
Lease takeover can reuse facts, not the old owner permission.

This is exact reference discovery, not global/latest discovery: no R2 listing,
prefix scan, mutable latest pointer or automatic selection of an old locator.
An absent or corrupt supplied locator blocks; it does not silently run sources.
The hash must be delivered by the existing trusted orchestrator/artifact channel;
this PR does not wire a new production scheduler or claim that channel is live.

## Bounds and resources

- Handoff body: 16 KiB client/server maximum; at most six references.
- Bundle objects: existing 1 MiB endpoint maximum.
- Keys derived from run hash, producer build, stage, input and content digest.
- Existing transport request ceiling (12), bounded HTTP and pre-spend allowance
  remain mandatory; no reset, retry loop or unbudgeted read/write is added.
- Each advancing prefix adds one POST (R2 Class A + exact R2 readback) under
  existing cost accounting. Recipient adds one GET before bundle downloads.
- No D1 writes, schema/migration, retention change or active pointer mutation.
- Budget unknown remains unknown; fixtures do not prove account capacity.

## Validation

Tests cover distinct host directories and a changed current owner, no source
rerun for a completed prefix, later-stage crash, missing/corrupt locator,
changed date/lock, invalid keys/hashes/size/schema, ambiguous resume inputs,
deterministic encoding, recovery refusal and 16 KiB server enforcement.
Existing lease, budget, lost-response and bundle validation tests remain.

Commands:

```
PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=
cd web
npm test && npm run lint
```

These are local integration fixtures, not provider or live canary evidence.
Final local results: Python 865 passed; Web 194 passed; build and lint passed.
Unknown budget prevents even the handoff read, independently tested.

## Remaining / stop

Publish checkpoint restoration still requires a correctly reconciled local
publication journal and fresh STAGED readback; this locator never fabricates
ACKNOWLEDGED intents. Recovery authority wiring, unfinished publication intent
handling, production caller/artifact-channel wiring, budget/lifecycle evidence
and actual canary/rollback remain incomplete. 080-RUNNER is not complete.

Stop after current-head CI for explicit merge + automatic Worker deployment
and pending migration check/apply authorization. No live task is permitted by
this code delivery.
