# 080-CANARY prerequisite: provider-period evidence contract

## Root cause and scope

PR #183 introduced bounded account reconciliation, but its cumulative-counter
regression checks interpreted both Actions and R2 through calendar `epochMonth`.
The authenticated official R2 usage surface instead shows a subscription billing
cycle. Calendar rollover could incorrectly permit an R2 regression, while a real
mid-month R2 rollover could incorrectly reject one. Rounded UI labels and storage
GB-months cannot be asserted as exact current counters/bytes.

Official contract reference: [Cloudflare billable usage](https://developers.cloudflare.com/billing/manage/billable-usage/)
states that usage is aligned to subscription billing cycles, not calendar months.
Private account usage values/exports are not included in this repository.

This repair extends the existing budget row and endpoint; no new ledger, table,
workflow, credentials, telemetry service or migration. It does not solve missing
provider telemetry by inventing numbers, changing precision or supplying zeros.

## Explicit opt-in and compatibility

Existing administrative `account_reconcile` accepts an optional explicit
`evidenceContract: flow-account-evidence-v2`. Its seven metrics must each contain
exactly `value,unit,basis,scope,measuredAt,precision,period`.

- `basis=measured`, `scope=account`, `precision=exact`; existing integer/unit,
  freshness, future-time and cap checks remain.
- `measuredAt` is the provider observation time, not a way to re-date stale billing
  data by reading the page now. Precision is an administrator attestation, not an
  independently authenticated provider signature. The source evidence hash must
  refer to truthful official evidence; hashing arbitrary JSON proves nothing.
- `period` contains exactly `kind,start,end`, canonical UTC ISO timestamps.
- D1 reads/writes and Free Worker requests: matching `utc_day`, midnight to next
  midnight. Actions: `calendar_month`, first day to next month's first day.
- R2 Class A/B: matching `billing_cycle`, explicitly supplied official start and
  end, 28–31 whole UTC days. No assumption of the account's cycle day is hardcoded.
- R2 current storage: `instant`, start=end=measuredAt. GB-months are not bytes.
- All cumulative intervals include start and exclude end. Admission verifies
  each interval still contains the current time; fresh evidence from an expired
  day/cycle is insufficient. An unsupported cycle shape is rejected, not guessed.

The stored account marks the same explicit evidence contract. Legacy-only callers
and stored rows retain legacy behavior; there is no bulk migration or silent
reinterpretation. An explicit legacy upgrade cannot lower prior cumulative usage
because its old provider period is unproven. Once upgraded, a fresh legacy request
cannot downgrade the account. Exact old receipt replay remains read-only.

Within the same provider interval, cumulative counters cannot regress. Different
intervals must not overlap or go backward; a new provider cycle may have a lower
counter. Storage occupancy may legitimately fall. Provider observation timestamps
cannot go backward. All reservations (including UNKNOWN), consumed/authorized
counters, settled charges and receipts remain intact across refresh and rollover.
Keeping settled charges can conservatively overcount; this repair does not free
holds or implement settlement reconciliation.

Dry-run remains one bounded primary-key read with no writes and
`executionAllowed=false`. Apply remains insert-if-absent or exact revision CAS.
Lease/owner/recovery approval fencing, task identity and actual settlement paths
are unchanged. GET exposes the explicit contract/periods only to the existing
authenticated caller; it does not grant task execution.

## Verification and remaining boundary

SQLite/real transition tests cover explicit initialization/dry-run/replay,
calendar-month crossing without R2 reset, mid-month R2 rollover retaining UNKNOWN,
overlap/backward/downgrade rejection, malformed periods/rounded counts/GB-months,
UTC end-exclusive admission, all seven caps, concurrent CAS and legacy upgrade.
Existing legacy tests are retained unchanged. Final Web lint/build and 235 tests
passed; complete root Python regression passed 1111 tests in 52.25s. The same
legal period fixture returns 422 on prior main and a read-only prepared response
on the fixed handler (writes 0). These are local tests, not production verification.
The lockfile was unchanged; npm ci reported 32 existing dependency vulnerabilities,
including one critical. No unrelated dependency upgrade is included.

This is code-contract repair, not official evidence acquisition, account apply,
candidate/canary completion, collection or production switch. Complete truthful
provider evidence is still required before preparation/apply. The current official
UI's rounded values must remain unusable for this exact contract; obtaining an
official precise export/API measurement is a separate read-only dependency, not
a new manual JSON approval gate.

## Actual merge effects

Base `e3633bf2f221d2680adbcc0182d03ade2d4b7214`; no open PR at initial check.
Changes under `web/**` cause the existing closed/merged PR deployment workflow
to run Web lint/build/tests, check/apply pending D1 migrations and deploy Worker
and static assets, then verify served build identity. No new migration in this
change; current pending production migrations must be checked before merge.
No flags, schedules, production read paths, data dates or active pointers change.
Merge/deploy and account apply are not authorized by local implementation.
The deploy job's configured timeout is 20 minutes; actual account capacity is not
inferred from this timeout or the successful prior deployment.
