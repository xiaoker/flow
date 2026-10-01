# 070-C Candidate UI/API Audit

Date: 2026-10-02 (Asia/Shanghai)

## Stage boundary

This change implements the v0.7 / 070-C candidate-only read workbench on top of merged 070-A and 070-B (`origin/main` base `8769d93485af63d751acae9936f29b103c18d87e`). It does not upload a candidate, activate a release, change the production read path or flags, run a collection/recovery/history task, or write D1/R2 data.

## Delivered behavior

- `/candidate` requires an operator to provide one exact `STAGED` `releaseId`; it never enumerates staged candidates and never reads the active-release pointer.
- The server performs one primary-key lookup in `release_manifest`, verifies schema-v4 candidate identity and manifest SHA-256, and can read only a declared artifact whose key is within that exact release prefix.
- Artifact reads are bounded to 5,000,000 bytes and validate D1 identity, R2 metadata, response identity, actual body length, SHA-256, JSON shape, and `asOf` before displaying data.
- Stock search and details stay within the selected penetration slot and manifest. Request generations cancel or discard stale responses after a selection/refresh change.
- The page distinguishes request date and actual `asOf`, capability state/source dates, period metric state/sample coverage, official node requirements, and the selected release identity/hash. It does not equate ETF sample counts with dates.
- Radar artifact count is shown as a whole-artifact count. It is not divided across periods: the last-60 series cannot prove that the nodes align with each requested period window.
- Backend exception text is not returned to the browser. Only recognized `candidate_*` error codes are returned; storage/provider messages become `candidate_read_failed`.

## Known limits and operator risks

- The current v4 manifest carries per-period status/sample counts/reason codes, but not a per-period list of exact missing official dates. This UI explicitly does not infer missing dates from the recent Radar series. Exact date gaps remain available from the bounded 070-B planner; joining that evidence to the candidate manifest/UI is not claimed by this PR.
- Authorization reuses the existing high-privilege `INGEST_API_KEY`. The key is entered into a password input, held by page JavaScript in memory, and sent in an HTTPS Authorization header; the application does not store it in URL or web storage. This is still a browser exposure of a privileged key. Use only a trusted, access-controlled device/entry point and clear the session when done. This PR does not implement a separate browser identity system and does not independently verify the Cloudflare Access policy protecting the page.
- No production candidate was read, no production D1/R2 budget was consumed, and no production deployment or UI acceptance was performed in this stage.

## Verification

Commands run from the isolated worktree:

```bash
PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q
cd web && npm run lint
cd web && npm test
git diff --check
```

Results: Python test suite passed; Web lint passed; Web test/build passed with 152 tests. Build emits the existing large-client-chunk advisory. A supplementary standalone `tsc --noEmit` was not treated as a passing gate because the repository has no configured typecheck script and reports unrelated baseline errors in unchanged files.

## Code review

The change was reviewed against [`docs/code-review-checklist.md`](../code-review-checklist.md), with findings recorded here rather than creating a second approval process. The review confirmed:

- Date, release identity, manifest hash, artifact size/hash, and payload `asOf` are checked before display; stale requests are discarded.
- D1 reads one exact `STAGED` primary-key row. R2 reads one declared key under the selected release prefix and enforces the 5 MB limit. The page does not enumerate candidates, poll, or write production state.
- Missing period-specific official-date lists remain explicitly unknown; Radar's recent-node count is not presented as coverage for each period.
- The existing `INGEST_API_KEY` remains a privileged browser credential while entered. The page keeps it in component memory and does not put it in URL or web storage, but this is still a material operator risk. Cloudflare Access protection was not independently verified and must not be assumed.
- Previous inspection fixes are retained: misleading period-node aggregation, mixed search/detail readiness, and raw storage-error disclosure.

The optional GStack review bundle file `.agents/skills/gstack/review/checklist.md` is absent; this does not block the project review because the repository checklist now defines the usable manual review. A screenshot-based browser audit was not performed and is not represented as complete. No production candidate was read and no production UI acceptance was performed.
