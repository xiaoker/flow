# 080-RUNNER: bounded core-collection adapter

Date: 2026-10-02 (Asia/Shanghai)

## Purpose

The v0.6-C entry is intentionally fixture-only: its request carries
`stageResults`, so a successful run proves the executor/checkpoint contract,
not that it can run the production collector. This change adds the first real
stage adapter without wiring it into the CLI, a GitHub workflow, Worker
schedule, publisher, or active-release path.

## Implemented boundary

- `CoreCollectionStageAdapter` calls the existing `collect_phase0` operation;
  it does not copy its parsing, source, or quality rules.
- The adapter requires exactly three explicit ascending business dates, an
  explicit collection window, the existing PCF cap, an explicit source
  fingerprint, a request ceiling no greater than 100, and at least two
  seconds between requests to the same host.
- Automatic HTTP retries are disabled. Redirects are followed one hop at a
  time so every network attempt consumes the request budget and host pacing.
  Cross-origin redirects fail closed if session credentials/cookies are
  present; request-level credentials are stripped before an allowed
  cross-origin hop. Budget exhaustion and HTTP 403/429 stop collection.
- The runner records the actual source request count separately from the
  allowed ceiling and rejects missing, malformed, or over-budget evidence.
- Output and receipt files are written only below the executor-provided
  stage workspace. There are no D1, R2, Worker API, GitHub dispatch, or active
  pointer operations.
- A successful local receipt binds run identity, input digest, manifest and
  quality file hashes. A resumed completed checkpoint re-verifies those
  files. An interrupted partial workspace without a receipt fails closed
  instead of fetching the same sources again.
- The adapter path requires an injected authorization verifier and a
  side-effect allowlist. The existing CLI and manual candidate workflow stay
  in fixture mode and do not instantiate this adapter.

## Explicitly not production-ready

This adapter is a first stage integration, not an 080-RUNNER canary. Before a
real run can be considered, the remaining pipeline stages need concrete,
reviewed adapters; checkpoint storage must survive the chosen execution
environment; and the caller must verify and renew the actual
`pipeline_window` lease while work is running. The current adapter does not
renew a lease, bind `target_date`, publish, or verify visible output. No
production collector or read path has been switched.

The request limit is a hard code cap, not a claim that a live account has
budget available. No official source was contacted during local tests.

## Side effects and deployment

- Local tests use a stub HTTP transport and temporary directories.
- If a trusted caller later injects this adapter, it may make at most the
  request count declared in its validated config and write only to its
  isolated stage workspace.
- This change contains no `web/**`, migration, workflow, or production-read
  path edits. The current `deploy-worker.yml` `web/**` path filter therefore
  does not select a Worker deployment for this PR. The repository PR CI does
  run the Python suite and web test/build/lint jobs; the branch-freshness
  workflow also runs. These consume Actions minutes.
- No candidate was uploaded, no production data was collected, no feature
  flag changed, and no D1/R2 or active-release state was touched.

## Local verification

- `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=` — **511 passed**.
- `cd web && npm test` — production build completed and **157 tests passed**.
- `cd web && npm run lint` — passed.
- `git diff --check` — passed.
- The Web build reports an existing bundle-size warning for a chunk over
  500 kB. `npm ci` also reported 24 dependency audit findings (1 low,
  6 moderate, 16 high, 1 critical); this PR does not modify dependency files
  or attempt an out-of-scope dependency remediation.
- Hosted CI has not yet been run. The implementation commit and final PR head
  will be recorded after the branch is pushed.

## Review record

- Scope: core-collection adapter, runner injection seam, fail-closed request
  errors, tests, and this audit record.
- Applicable review: source/resource bounds; date/input identity; checkpoint
  artifact hash binding; authorization before stage effects; protected output
  paths; merge/deployment side effects.
- Evidence: local tests above, request redirect/count tests, source 429 stop
  test, tamper-detection test, and the unchanged `web/**` deployment filter.
- Findings: no new migration, remote database write, workflow/Worker wiring,
  production read change, or data collection occurred. Live lease validation
  and renewal, durable cross-job state, remaining stage adapters, deployment
  verification, and production canary remain unimplemented/unverified.
- Merge effects: the PR should run CI and consume Actions minutes; it does not
  match the Worker deploy workflow's `web/**` path filter. No production
  deployment or migration is expected from this diff.
