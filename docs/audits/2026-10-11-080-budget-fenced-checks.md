# 080 candidate budget lease-check convergence

Base: b1fa757 (PR #179). Candidate-only; no runner/workflow/production activation.

The fixed shared-budget endpoint already verifies live execution identity on
GET and includes the lease/recovery approval fence in the final POST CAS
(`web/app/lib/pipeline-shared-budget.ts`). Repeated client `confirm()` calls
around those requests duplicate control-plane reads without making the SQL
commit more atomic.

An explicit `server_fenced_checks=True` policy uses guard `check()` instead.
It still fails for unverified/expired/deadline-exceeded leases and performs
due renewals. The default remains fresh confirmation. No request cap changes.
Checkpoint transport confirmations, outcome-unknown holds, receipt validation,
CAS revision checks and replay restrictions remain unchanged.

The declared preflight uses the matching policy. Two checkpoint operations
without renewal cost 10 lease requests and 16 total Worker requests instead
of 26 and 32. Three additional budget transitions, two runner confirmations
and one renewal upper bound yield 17 lease / 29 total requests. These are
fixture/declared envelopes, NOT complete runner costs, provider telemetry,
account balance, or authorization to execute.

Validation: Web 215 tests, lint and build passed. Full Python includes real
HTTP/SQLite ordinary and recovery admission in both policies; final result
is recorded in the PR report. Regression proves due renewal detects revoked
approval before any budget or object request. Hosted CI must pass before merge.
Full runner lifecycle and trusted entry remain unfinished; no collection or
production recovery ran. Actual provider rows and account capacity are UNKNOWN.

Merge triggers main CI only, no Worker deploy or migration. See the adjacent
merge-side-effects JSON. Original protected paths remain untouched.
