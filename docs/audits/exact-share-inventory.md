# Exact ETF-share archive inventory

The existing manual stock-flow inventory workflow accepts optional
`exact_slots_json`: an array of one to six unique objects containing only
`date` (a valid ISO calendar date) and `source` (`sse` or `szse`).

When supplied, the workflow skips Wrangler installation and the D1 date query.
It probes only ETF-share metadata for those slots, serially, with one attempt
per slot. It does not probe PCF or infer trading days. The dates must come from
the caller's existing official calendar evidence.

Existing GitHub Secrets authenticate requests to the fixed Flow HTTPS origin.
Redirects are not followed. Response bodies are not consumed, and each response
is closed. Unknown, authentication or invalid-metadata results stop the remaining
queries; a 404 is recorded as missing from this route, not proof that raw source
data does not exist. The artifact retains the full requested scope and whether
all slots were examined. Metadata readiness does not prove payload hashes, QA,
period coverage, or successful recovery.

Maximum six HTTP requests. On the bounded unindexed share route this entails at
most twelve R2 list calls (1,536 summaries), six object reads, and no D1 query.
Worker streaming/provider accounting is not claimed to be zero merely because
the client does not consume the body. The existing 20-minute job timeout remains
the Actions ceiling. No production writes, collection, publication, flags or
active-pointer changes are introduced. The legacy date-range mode is unchanged.

Merging this Python/workflow/docs change runs CI but does not match the Worker
deployment path filter. Running the manual workflow is a separate bounded
production-read action, not implied by code merge.
