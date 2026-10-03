# 080-RUNNER publication contract extraction

Base: `5f793b7bf04014bb4637fa867140658074346bb3`, merged PR #160. Main CI `37138580284` succeeded.

## Scope and equivalence

Move the existing upload-body construction and capability response validation from `scripts/publish_read_models.py` into `trading_desk.pipeline_publication`. The script imports the same function object and keeps its public imports and CLI compatible. The candidate runner can now reuse these contracts without importing an executable script or duplicating publisher business rules.

No transport, retries, HTTP timeout, redirect handling, metrics, request-size limit, artifact hash check, final result validation or activation behavior changes. Gzip's existing timestamp behavior is deliberately preserved; tests freeze time only for exact wire comparison. Schema-v4 still requires explicit stage_only. The pure helper builds a request but does not establish authenticity, resource admission, lease authority or publication success.

No new runner publish caller is enabled. This is the shared contract prerequisite, not a completed publish adapter. Candidate publication transport must still be bounded, authenticated, budget-accounted and lease-fenced; ambiguous writes need reconciliation before retries or resume. Recovery, lifecycle acceptance and separately authorized canary remain pending. 080 is CODE_IN_PROGRESS.

## Verification and review

- Targeted old publisher/shared contract tests: 32 passed.
- Full Python: 782 passed (`PYTHONPATH=src:. .../.venv/bin/python -m pytest -q -o addopts=`).
- Web build/tests: 177 passed; lint passed (results confirmed before PR).
- Tests cover all existing slots and schema versions, missing-capability rejection parity, exact request fields/compressed payload, active/candidate CLI results, purity and rejection before HTTP.
- Fixtures are local compatibility evidence, not production acceptance or a source data claim.
- Manual applicable `docs/code-review-checklist.md` sections 3–6: no formula/date/auth changes, no lowered gate, no added side effects. Existing chunk-size warning is unchanged. Account quota/production acceptance remain UNKNOWN.

Only source/script/tests/docs change: PR and main CI, no Worker deployment or migration path match, no collection, candidate upload, active pointer, flags or retention changes. Protected original user files untouched. Code revert restores the old module location; there is no data mutation to undo.

Stop after hosted CI for merge authorization. After merge/main CI, continue the existing candidate publication seam from fresh main; do not copy these functions back into a second publisher or activate production.
