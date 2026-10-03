# 080-RUNNER: local budget persistence

Base: `48d1e6b86931b41d50d54ff63e2d589a4b1fa177` (PR #151).

## Scope and limits

`LocalBudgetLedger` is an opt-in local adapter of `BudgetLedger`, not a second
budget policy or production datastore. SQLite is only for the controlled local
execution environment. Independent processes sharing **one local file** reuse
the existing reserve/authorize/debit/settle/expire transitions atomically.
Separate Actions runners, hosts or copied databases do NOT share reservations.
No production caller, CLI default, workflow, D1 schema, flag or read path changes.

Explicit exclusive initialization creates the file with mode 0600. Reopening a
missing, truncated, corrupt, different-engine or changed-evidence store fails
closed. An evidence epoch binds all limits, original measurements and TTLs.
Measured freshness remains the original timestamp: restarting does not refresh
account telemetry. There is no reset, pruning or automatic epoch rollover.

Each operation takes `BEGIN IMMEDIATE`, checks the immutable contract, replays
the bounded hash-chained journal into the existing state machine, then commits
the event and head in one transaction before returning allowance. Invalid
operations roll back. `UNKNOWN`, expiry and ambiguous I/O keep their holds.
Reconciliation uses measured settlement through the existing state machine.
Hashes detect corruption/truncation, not malicious modification by a local user
who controls both the file and application; filesystem access is a trust boundary.

Journal caps: 2,048 events, 16 KiB per new event, 2 MiB payload total, 5-second
SQLite lock timeout. It is not a network-filesystem coordination protocol.
Capacity, lock timeout or commit error denies permission, never clears state.
The bounded replay avoids maintaining a second copy of transition semantics.
At cap, explicit reconciliation/export and a reviewed lifecycle transition are
still required; callers must not initialize a new database to bypass spent quota.

## Verification

Local tests cover restart/idempotence, 20 independent OS processes with only one
winning allowance, UNKNOWN/expired holds, measured settlement without double
counting, absent store, exclusive initialization, changed evidence, corruption,
truncation, capacity, rollback and clock regression. Fixtures use no production
credentials, provider calls or remote writes.

Full Python: `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python
-m pytest -q -o addopts=` — 624 passed, including 17 adapter tests. Web:
`npm test` — build succeeded and 166 passed; `npm run lint` and `git diff --check`
passed. Hosted CI is recorded in the PR. No live canary or production acceptance
is claimed. The independent review's configuration mutation finding was fixed
by checking live configuration against the durable contract before each replay;
mutation, stale evidence, commit rollback and abrupt exit tests were added.

## Remaining before 080-RUNNER canary

- Distributed account-wide reservations across GitHub/local execution, including
  actual known available budget and accounting overhead, remain unimplemented.
- Local adapter persistence alone does not authorize checkpoint HTTP requests or
  other live work. Lease authority and full-task allowance remain required.
- Checkpoint retention/restore acceptance, real sizes versus the 1 MiB transport
  cap, remaining real adapters, recovery integration and rollback remain pending.
- No provider quota guarantee, completed 080-RUNNER or production takeover.

Merge runs PR/main CI only. No Worker deployment, migration, collection,
candidate write, flag or active pointer mutation. Protected original files stay
untouched. Stop after hosted CI for merge authorization.
