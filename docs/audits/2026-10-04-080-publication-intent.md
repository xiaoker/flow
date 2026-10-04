# 080-RUNNER: local durable pre-effect publication intent

Base: PR #164 merged as `5a78ab0f7c4a736b496c10a2fae27eb4e9975515`.
Branch: `codex/080-publication-intent-20261004`.

## Scope and trust boundary

Adds an explicitly injected, local SQLite intent store to the existing ordinary
candidate transport. No production caller, CLI, workflow, remote table or new
authentication system is registered. Existing callers remain unchanged.

The file must be explicitly initialized once and retained by the caller. All
resuming processes must use that same file. It is not a cross-host coordinator;
creating a different file does not prove a previous upload never happened.
Current lease guard and shared budget remain authoritative. This local receipt
is not fresh remote visibility evidence and never authorizes activation.

Binding includes runKey, targetDate, producerBuildSha, owner, attempt, release,
manifest hash and exact artifact identity. Gzip wire timestamps are deliberately
not part of the immutable identity. Raw content and secrets are not stored.

## Ordering and uncertainty

1. Validate signed producer bytes, identity and declared artifact.
2. Check existing local receipt; acknowledged exact receipt can be reused after
   lease confirmation without another HTTP request.
3. Authorize existing resource allowance and confirm current lease.
4. Commit PENDING intent with SQLite synchronous FULL before HTTP.
5. Perform one bounded upload and validate its exact receipt, confirm lease,
   commit ACKNOWLEDGED, then clear the instance-local hold.

Any ambiguous response or acknowledgement failure retains PENDING. Restart does
not clear it. Final upload can use the existing exact STAGED readback API;
successful hash/date/content verification durably acknowledges the same intent.
Missing or mismatching readback does not grant retry permission. Intermediate
unknown writes still block: their orphan reconciliation surface is not yet
implemented. No retry, refund, cleanup, pointer change or evidence fabrication.

BEGIN IMMEDIATE serializes competing local instances. At most 40 intents, each
canonical record/receipt at most 16 KiB; lookup reads at most 41 indexed rows.
Missing files, symlink paths, binding conflicts and digest corruption fail closed.
Digests detect accidental corruption, not malicious users rewriting their own
SQLite database. Filesystem isolation and retention are caller requirements.

## Validation

Eight new tests cover commit-before-HTTP, restart receipt reuse, lost response and
exact readback, acknowledgement disk failure, 20 concurrent instances with one
winner, separate-process observation, missing/corrupt/symlink/identity rejection,
intermediate unknown block, capacity and receipt conflicts.

Commands: full Python `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`;
Web `npm test` (includes build), `npm run lint`; `git diff --check`.
The actual loopback HTTP fixture requires local socket permission. Fixture
results are not production Access, account budget or live publication evidence.
Results: full Python 833 passed; new intent tests 8 passed; Web 191 passed,
build and lint passed. Diff whitespace check passed.

## Remaining and effects

080 remains CODE_IN_PROGRESS: runner adapter registration, durable transport
across hosts, intermediate unknown-write reconciliation, recovery wiring and
live acceptance remain. No collection, production upload, migration, flag,
active pointer, retention or protected original-worktree files were changed.
PR/main CI consumes Actions minutes; Python/tests/docs paths do not trigger the
Worker deployment path filter. Stop after green hosted CI for merge authorization.
