# 080-RUNNER lease-safe isolated bundle resume

Base: `df47766f2539c376f468964b40f699089d953e6e` (merged PR #146).

The in-process candidate runner now accepts an explicit `resume_bundles` mapping
and `export_bundles` option. Both require stage adapters and an independently
verified lease guard; the CLI and workflows do not expose or enable them.
No production scheduling or read path changes.

## Safety contract

- Existing application identity and self-hashed checkpoint remain authoritative.
- Runner binding additionally includes dependency lock digest, ordinary/recovery
  mode and recovery identity/plan. Producer lease owner/attempt are audit evidence;
  an authorized takeover can restore under its own verified current lease.
- All bundle/file hashes and the exact stage/config digest are checked before
  any restore writes. The expected outer digest must come from an approved
  source; a caller-created digest alone is not proof of production approval.
- Restores use a new isolated stage directory, reject symlink ancestors and
  protected paths, and never merge into existing data. Every file is exclusively
  created, flushed and fsynced; lease authority is confirmed before writes and
  after the adapter independently verifies output evidence.
- Interrupted restoration leaves an uncommitted partial directory for diagnosis.
  A retry cannot overwrite it. Only runner checkpoint persistence commits stage
  completion after successful restoration and a fresh lease confirmation.
- Export uses content-addressed bundle filenames. Existing bytes must match;
  symlinks/collisions fail closed. A fresh lease check precedes bundle persistence
  and checkpoint persistence. Outputs remain bounded by PR #146 limits.
- Restoring into a job with local checkpoint state is rejected rather than
  resolving potentially conflicting evidence silently.

## Validation / boundaries

Fixture tests demonstrate two independent job directories, zero adapter reruns
on verified resume, takeover owner/attempt, lease loss, partial restore rejection,
corrupt bytes, adapter rejection, changed parser input, execution binding mismatch,
symlink refusal and missing authority. These are not production or R2 evidence.

Final local verification: complete Python suite 564 passed (11 new resume tests),
Web build and 159 tests passed, lint passed, diff whitespace check passed.
Commands: `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q -o addopts=`,
and in `web`: `npm test`, `npm run lint`. No assertion was weakened or skipped.

This PR changes Python code/tests/docs only; no migration or Worker deploy path
is touched. PR/main CI uses Actions minutes. No remote R2/D1 writes, official
requests, flag changes, active pointer changes, recovery or canary were run.
Ordinary lease guard renewals remain possible only for a separately authorized
in-process live caller, and retain their existing reported side effects.

## Next

Add bounded authenticated immutable R2 transport with trusted exact-key references,
request/byte accounting, authority checks and write/readback ambiguity handling.
Do not claim durable resume is complete: current report explicitly says
`durableTransport=not_connected`. Then finish remaining adapters, recovery/budget
integration and the separately authorized canary package.
