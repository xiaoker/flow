# 080 bounded candidate publication transport

## Scope and result

The injected Python `CandidatePublicationTransport` uses the existing signed
candidate body builder, ordinary execution identity, lease guard, shared budget
ledger, bounded HTTP exchange and existing admin endpoints. No new authentication,
publication formula, database protocol, CLI/workflow registration or activation
path was introduced. This increment does not complete 080-RUNNER.

- POST only to `/api/v1/admin/read-model-import`, always v4 `stage_only`, with
  original signed producer JSON and bound execution identity.
- Full known per-request resource estimates and reservation required before
  lease/HTTP; shared ledger validates run/owner/attempt/date/task identity.
  Endpoint operation floors are estimates, not physical D1 measurements or
  proof of a sufficient whole-task budget. Caller includes lease/control and
  execution overhead. Account remaining and endpoint physical D1 counters stay
  UNKNOWN; shared budget control-plane evidence remains separate.
- HTTPS origin restricted to Flow; explicit localhost fixture exception only.
  No redirects or hidden retries. At most 40 requests per instance, 20 seconds
  per exchange, 1.5 MB decoded response, bounded chunk count and serialized calls.
- Wrong HTTP status, malformed/wrong receipt, transport failure, late lease loss
  all retain unknown write outcome. No automatic readback, retry or refund.
- Only the same final-upload execution/manifest/artifact can explicitly reconcile
  via the STAGED-only candidate API, exact manifest hash, byte/hash/date/status
  headers and exact body. Failed/missing readback cannot clear uncertainty.
- Intermediate orphan uploads cannot be reconciled through that existing API;
  they remain blocked, never treated as absent or retried blindly.

## Verification

Full Python: 825 passed, including 24 new cases, through the repository's real
pytest command. Initial sandbox run failed two existing localhost socket tests;
the full suite was rerun with localhost binding permitted, without skipping them.
Web `npm test` (production build + 191 tests), `npm run lint` passed.

Coverage includes exact signed bytes, stage-only receipt, shared authorization
and identity mismatch, unknown/understated budget, concurrent request ceiling,
real localhost HTTP/auth/wire flow, response deadline/oversize, status failures,
wrong receipt, final exact readback, failed readback, separate readback allowance,
intermediate uncertainty and late owner. Fixtures are not production acceptance.
Existing large-client-chunk build warning remains; no new Web code changed.

Manual review used `docs/code-review-checklist.md`: relevant date/hash/authority,
bounded queries/requests, UNKNOWN resource evidence and production side effects
reviewed. Browser/UI acceptance N/A to this injected Python-only change.

## Remaining ordered work

1. Wire publication into the existing runner with persistent pre-effect intent,
   uncertain-write state and exact receipt/checkpoint verification across jobs.
   This transport's pending hold is instance-local; it does not claim durable
   restart safety. New instances must not be used to bypass an unknown write.
2. Resolve the intermediate-upload reconciliation seam through a bounded existing
   authority contract before any live runner canary, not a full bucket scan.
3. Complete recovery authorization integration, whole-task resource evidence,
   rollback and exact ordinary-window live canary package; no production entry
   is enabled by this increment. Recovery mode explicitly remains unsupported.

## Merge side effects

Only Python/tests/docs: PR and main CI consume Actions minutes. No Worker deploy,
pending D1 migration apply, flag, active pointer, collection, candidate upload,
retention change or remote production operation. Original protected paths and
other user files untouched. Stop after CI for merge authorization. Live quota
and candidate Access acceptance remain UNKNOWN and not prerequisites for local
code work, but must be verified for any subsequently authorized live operation.
