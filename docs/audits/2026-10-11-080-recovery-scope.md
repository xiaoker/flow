# Recovery runner scope boundary

Base: PR #170 merge `4e1f93348fae16b138aaa08917c3fa56baa8dae2`; main CI 38065767394 passed. No deployment required.

Confirmed gap: injected recovery adapters previously relied on the context's plan hash and independent authorization callback, but never consumed the actual immutable plan to constrain requested application stages. This PR adds the missing local fail-closed boundary before any callback, adapter, checkpoint read or filesystem write.

`approved_recovery_plan` is explicit in-process input, not approval. The existing plan validator verifies semantic hash, recovery ID, run key, exact true target date and deadline. Only stage names with identical existing semantics (`core_collection`, `publish`) can be bound; requested stages must be a subset of the approved plan. Product domains such as dashboard/stock_flow do not implicitly authorize generic normalize/build/verify adapters. Unproven mappings reject. Ordinary live callers and fixture mode remain unchanged.

The returned pure evidence deliberately says liveAuthorityVerified=false. Live adapters still require the existing independent authorization verifier/lease guard. Ordinary-only budget, checkpoint and candidate publication restrictions remain intact. No remote recovery execution is enabled. A full recovery runner needs domain-specific adapter mappings plus current approval/lease proof, shared allowance and recovery-fenced immutable transport. This PR does not claim those are done.

Tests cover identity/hash/mode/auth mismatch, true target vs scheduling window, deadline equality, widened/unknown/duplicate stages, dashboard/build confusion and runner rejection before callbacks or state creation. Complete test results belong in the PR report.

Next: extend the existing bounded lease/control-plane contracts using exact recovery binding and approved domain-specific adapters; do not remove ordinary guards. Then complete resource/transport integration and separately authorized acceptance. No collection, registration, flag mutation, activation or canary in this PR.
