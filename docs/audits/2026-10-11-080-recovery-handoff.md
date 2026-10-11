# 080-RUNNER: scoped recovery checkpoint handoff

Base: `ce0c56e2992aee959a8b0bcca745da468743dce0` (PR #177).

## Scope

Reuse the existing immutable locator and checkpoint endpoint. Recovery locators
bind recovery ID, approved plan hash, scope hash, date, build, lock and stage inputs.
References must be a nonempty stage prefix in the exact recovery namespace.
Owner/attempt is not part of immutable object identity; current lease and approval
are still checked before and after each remote operation. No locator grants authority.

The server checks immutable plan approval for every requested stage. Dashboard
maps only to build/verify. Limits remain 16 KiB per locator and 1 MiB per bundle.
No new endpoint, scheduler, flag, migration or production caller is added.

## Validation

- Python: 963 passed, 20.71s, full `pytest -q -o addopts=` with `PYTHONPATH=src:.`.
- Web: `npm test` production build and 215 tests passed; `npm run lint` passed.
- Targeted Python transport/locator suite: 34 passed. Real SQLite server fixture
  checks bounded indexed queries and zero D1 writes; scoped GET/PUT and mutated
  hash/date/namespace/approval failures are covered.
- Tests are local fixtures, not live recovery or account quota evidence.

## Remaining limits

The fixture's same-host upload/upload/download sequence exhausts the existing
32-request lease cap because budget operations also confirm authority. It fails
closed. Cross-host tests use a fresh guard and allowance for the reading instance;
this does not prove a complete multi-stage recovery fits that cap. No cap was
raised. Full task request/cost preflight and lifecycle remain required before a
real run. The alternate owner hash test proves only immutable identity stability,
not a live lease takeover.

080-RUNNER remains CODE_IN_PROGRESS: trusted formal entry, complete lifecycle/cost
proof, approved real canary/rollback and ten-day acceptance remain. Account usage
and remote pending migrations are UNKNOWN. Merge auto-deploys Worker and checks
pending migrations; await explicit combined approval. Original protected paths
were not edited. No collection, recovery approval, pointer switch or deployment
was performed by this code task.
