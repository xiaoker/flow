# 080-RUNNER exact candidate manifest bytes

Base: PR #162 merge `860d7673f8e8dd85649ff078bc85864e24f4c75b`. Main CI `37172358522` and automatic deployment `37172358765` passed; authenticated deployed SHA verified, no pending migrations. Isolated branch `codex/080-publication-transport-20261004`.

## Finding and bounded repair

The existing upload route serializes the parsed manifest using JavaScript JSON.stringify. The producer signs Python canonical JSON; a decimal `1.0` becomes `1` in JavaScript. The existing candidate reader deliberately hashes exact producer bytes and cannot accept that transformation. This is a reproduced compatibility defect, not evidence that a current production data release contains that number or is broken.

Add pure `build_candidate_upload_body` in the existing shared publication module, reusing `canonical_json_bytes`, `build_upload_body` and their signing/compression semantics. It validates signed canonical manifest bytes (64 KiB), artifact digest/size, date/build equality and request bounds, and carries `candidateManifestJson` plus the existing explicit pipelineExecution. It does not establish lease authority, producer provenance, budget admission or publish success.

When the optional candidateManifestJson field is supplied, the existing upload endpoint requires explicit runner authority, semantic equality with the validated manifest, bounded bytes and the existing `candidateManifestSha256` check. The exact signed producer string is persisted unchanged to immutable R2 and lease-conditioned STAGED D1 storage. No new serializer, date/hash relaxation, auth system, table, endpoint or feature flag. Legacy callers without this field retain prior behavior; no runner caller is currently enabled.

The future candidate transport must always use this byte-preserving helper. Compatibility for earlier explicit requests without the field does not establish exact-byte proof; the existing candidate reader still fails closed on a bad signed manifest. R2/D1 are not a cross-service transaction; immutable orphan objects still never imply completion or activation.

## Verification / review

- Full Python suite: 801 passed, including signed decimal byte preservation, tampering, noncanonical/duplicate/oversized input, artifact/date/build mismatch and request bounds.
- `npm test` builds the Worker and passes 191 tests; `npm run lint` passes. Built endpoint test verifies a signed decimal fixture reaches exact STAGED storage and malformed/mismatched/unleased byte input performs no writes.
- Original legacy publication tests continue to use the same build_upload_body function and unchanged default activation behavior. No CLI or workflow registration.
- Review checklist 1, 3–6 applied; physical D1 telemetry, account budget and production candidate acceptance remain UNKNOWN. Local fixtures are not live validation. Existing chunk-size warning unchanged.

## Effects / next action

Changes under web/** trigger automatic Worker deployment and pending D1 migration application on merge. No new migration; current remote pending migration state UNKNOWN. See companion side-effects JSON. No upload, collection, flags, active pointer, retention, remote migration or protected user file modification during implementation.

Stop after code PR CI for explicit merge/deploy authorization. 080 remains CODE_IN_PROGRESS. Next finish the bounded candidate transport and runner adapter with shared budget/lease checks, exact STAGED/artifact readback and ambiguous-write reconciliation. Do not build a second publisher or infer runtime authorization from the pure helper. Recovery integration and separately authorized live acceptance remain.
