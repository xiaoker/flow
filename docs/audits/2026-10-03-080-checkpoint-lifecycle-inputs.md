# 080 checkpoint lifecycle input consistency

Base: `e73633c8f74e22afb68d319e74c30c43d3b18830` (PR #154).

This candidate-only pure helper checks caller-supplied lifecycle evidence before
offline checkpoint sizing can report LOCAL_INPUTS_CHECKED. No evidence means
BLOCKED, even when sizing and resource inputs pass. It never calls a provider,
installs an expiration rule, lists a bucket or deletes anything.

The evidence schema includes schemaVersion=1, exact checkpoint prefix,
observedAt, evidenceTtlSeconds, bucketLockAbsent, expirationRulesComplete,
expirationRules, maximumResumeAgeDays, referenceAudit and restoreEvidence.
expirationRules is a complete normalized snapshot of enabled expiration rules,
not a filtered list of only matching rules. Parent/child overlap is rejected.
The dedicated prefix is `market-data/pipeline/checkpoints/v1/`; release, raw and
historical data prefixes are not eligible. Rules are bounded to 32 entries;
TTL is at most 24 hours, retention is 1–90 days, and the declared maximum resume
age must be strictly less than retention. These are candidate consistency
ceilings, not a production retention decision. The seven-day test is synthetic.

Reference evidence must cover the complete prefix, be fresh and have a measured
zero atRiskCount. Unknown, bool-as-zero or nonzero risk blocks. Fresh restore
evidence is required. Caller assertions are not authenticated Cloudflare proof:
even a passing report always has productionVerified=false,
liveExecutionAllowed=false and policyMutationAllowed=false. The existing
verified_checkpoint_lifecycle requirement remains outstanding for live use.

No production reads, writes, migration, schedule, flag, pointer or protected
original-file changes. CI consumes Actions minutes; no Worker auto-deploy matches
these Python/tests/docs paths. Independent review found no actionable issue.

Verification: targeted 43 tests and full Python 683 tests passed.
Web 177 tests, build and lint passed. Two local socket
fixtures require execution outside the listener-restricted sandbox; they are
not production network tests and are not skipped.

Next: after CI and merge authorization, continue lifecycle provider evidence,
shared-account evidence reconciliation, full-task bounds and remaining adapters.
Do not install retention or run a canary in this code PR. 080 is not complete.
