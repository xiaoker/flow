# Completed publication checkpoint restoration

Candidate-only, explicit `restore_completed_intents=False` by default. No production caller is registered.

An imported completed publish receipt cannot supply another owner's upload authority. Under the new owner's existing guarded transport and shared allowance, validate the exact local checkpoint and every local intent, read every declared artifact from the STAGED candidate endpoint, then create current-owner local acknowledgements. All remote reads must pass before the first local record is created. Each local write is preceded by current lease confirmation. No candidate upload, finalize, activation or remote marker deletion occurs.

Missing/corrupt candidate, denied budget, wrong checkpoint, default-disabled mode, or a local pending intent cannot become an acknowledgement. A crash between local begin and acknowledge remains pending and requires the existing explicit reconciliation; this code does not clear unknown state. Transport hard request ceilings and the shared budget remain applicable.

Tests include fresh-host/new-owner reconstruction with zero upload requests, absent/corrupt remote evidence, allowance denial, default-off behavior, checkpoint mismatch, and pending intent rejection. These are local behavioral fixtures, not production acceptance.

PR #169 merged a9878d47c9a78d55ab9c037ee766598eefc75dbd; main CI 38064627849 and deploy 38064628262 succeeded. Exact build identity was verified; no migrations to apply.

Remaining: recovery authority integration, trusted production artifact channel/callers, actual account/lifecycle evidence and separately authorized single-window canary/rollback. 080-RUNNER is not production-complete.
