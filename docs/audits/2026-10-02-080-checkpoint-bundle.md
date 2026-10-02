# 080-RUNNER immutable bundle prerequisite

PR #145 is merged at `e72d2503fa12a61d72d828eb25b5653ddf5a496c`.
This increment deliberately implements only the transport-independent bundle
contract, not durable storage or production resume.

The existing application checkpoint supplies exact runKey/windowDate/targetDate,
producer SHA, stage, input/output digests, effects and evidence. Its self-hash
must validate and its status must be COMPLETED. Every regular file under the
isolated stage workspace is included; links, unsupported file types, protected
paths, traversal, duplicate or conflicting paths are rejected. A canonical
manifest hash and byte digest bind the complete bundle. Verification is pure
and checks all files before returning any content to a future restore caller.

Hard limits: 256 files, 512 visited entries, 8 MB per file, 24 MB decoded total,
34 MB encoded bundle. Limits reject rather than silently truncate. File content
is never interpreted as executable code. The caller must supply a trusted
expected digest; a self-hash alone does not establish authenticity.

The existing history and release artifact routes have domain-specific date and
release semantics. They are not reused as an arbitrary checkpoint write API.
No second lock, D1 table, active pointer, workflow or remote endpoint is added.

## Remaining work, in order

1. Integrate bundle export/import into the candidate runner under verified lease
   authority, with safe atomic isolated restore and adapter receipt verification.
2. Add bounded authenticated immutable R2 transport using existing storage and
   admin auth; verify write/readback ambiguity and exact-key reads. No bucket scan.
3. Prove two separate job workspaces resume without re-requesting official sources.
4. Finish remaining adapters, recovery authorization, budget admission and the
   separately authorized single-window canary. CLI/workflows remain fixture-only.

This PR has no migration and no `web/**` or deploy workflow change. Creating a PR
runs Actions CI; merging runs main CI but does not match Worker deploy filters.
No official requests, production verification, data recovery or quota claim is
made by local fixture results. Original protected user files are untouched.
