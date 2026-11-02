# Bounded history artifact discovery

The legacy date-only lookup is a discovery convenience, not an inventory or a
recovery-completeness certificate. It lists at most the ISO and compact date
prefixes, once each, with 128 summaries per prefix. Both complete pages may be
full (256 summaries total). A truncated or unproven page returns
`413 artifact_candidate_limit_exceeded`; it must never be interpreted as absence.

The cap is an internal request bound, not a Cloudflare free-tier quota or a claim
that every production prefix fits. Actual unresolved-prefix cardinality remains
unknown until bounded production evidence is obtained.

Prefer existing indexed pointers or exact manifest key/hash references. The
authenticated `archive_key` + `archive_sha256` path reads one exact object,
verifies its bytes, and does not list R2 or query D1. An oversized discovery
prefix therefore does not prohibit a valid exact-reference read. Do not increase
the cap or discard hash checks to force discovery through.

Regression coverage includes complete pages at the cap, truncated/unknown pages,
scope rejection, route error mapping, and exact-reference discovery bypass.
Fixture tests do not demonstrate production availability or restored periods.

This change adds no migration. Web changes trigger the existing deployment and
pending-migration workflow only after an explicitly authorized merge. No data
collection, history write, recovery approval, flag change, or activation is
performed by this PR.
