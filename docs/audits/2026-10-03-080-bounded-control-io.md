# 080 candidate bounded control-plane I/O

Base: `7532a2538e50f5c0938deac2f84651923abf1e83` (PR #153).

The shared budget and checkpoint clients now reuse a bounded HTTP exchange.
Each exchange has a foreground deadline of at most 20 seconds, body byte and
chunk limits, no redirects and no automatic retries. This is a per-exchange
bound, not proof of a whole-pipeline execution bound.

A timeout does not cancel the remote operation. The original daemon may finish
later; it cannot report a late success to the caller. While it remains alive,
another exchange is rejected. An uncertain checkpoint POST blocks another POST
until an explicit readback verifies the same descriptor, object hash, bundle
identity and valid lease. Failed readback does not erase uncertainty.

Incomplete response measurements remain sticky lower bounds. D1 write usage
without telemetry is unknown, not zero. Budget admission semantics are unchanged.

Validation: focused tests 44 passed; full Python suite 652 passed; Web tests
177 passed; Web lint and build passed. Independent read-only review found no
actionable correctness or security defects. These are local fixture tests, not
production acceptance. Existing build chunk-size warnings remain.

No production calls, migrations, flags, pointers, collection or retention changes.
The original worktree and its protected reports/memory files were not modified.
080 remains in progress: lifecycle, evidence provisioning/rollover, full-task
resource bounds, remaining adapters and authorized live acceptance are not closed.
