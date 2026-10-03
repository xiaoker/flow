from copy import deepcopy
from datetime import datetime, timezone

import pytest

from trading_desk.pipeline_checkpoint_lifecycle import CHECKPOINT_PREFIX, checkpoint_lifecycle_preflight

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def lifecycle_fixture():
    # Synthetic evidence only, never a statement about production R2.
    return {"schemaVersion": 1, "prefix": CHECKPOINT_PREFIX,
            "observedAt": NOW.isoformat(), "evidenceTtlSeconds": 3600,
            "bucketLockAbsent": True, "expirationRulesComplete": True,
            "expirationRules": [{"prefix": CHECKPOINT_PREFIX, "expirationDays": 7}],
            "maximumResumeAgeDays": 6,
            "referenceAudit": {"prefix": CHECKPOINT_PREFIX, "complete": True,
                               "observedAt": NOW.isoformat(), "atRiskCount": 0},
            "restoreEvidence": {"prefix": CHECKPOINT_PREFIX, "verified": True,
                                "observedAt": NOW.isoformat()}}


def test_local_consistency_is_deterministic_and_never_authorizes_policy(tmp_path):
    evidence = lifecycle_fixture()
    before = deepcopy(evidence)
    report = checkpoint_lifecycle_preflight(evidence, now=NOW)
    assert report == checkpoint_lifecycle_preflight(evidence, now=NOW)
    assert evidence == before
    assert list(tmp_path.iterdir()) == []
    assert report["status"] == "LOCAL_POLICY_INPUTS_CHECKED"
    assert not report["productionVerified"]
    assert not report["liveExecutionAllowed"]
    assert not report["policyMutationAllowed"]
    assert not any(report["sideEffects"].values())


@pytest.mark.parametrize("field,value", [
    ("schemaVersion", True), ("prefix", "market-data/"),
    ("bucketLockAbsent", None), ("bucketLockAbsent", False),
    ("evidenceTtlSeconds", True), ("evidenceTtlSeconds", 86401),
    ("observedAt", "2026-10-01T00:00:00Z"),
    ("observedAt", "2026-10-04T00:00:00Z"),
    ("expirationRulesComplete", False), ("expirationRules", []),
    ("maximumResumeAgeDays", 7), ("referenceAudit", None), ("restoreEvidence", None),
])
def test_missing_or_incompatible_evidence_blocks(field, value):
    evidence = lifecycle_fixture()
    evidence[field] = value
    assert checkpoint_lifecycle_preflight(evidence, now=NOW)["status"] == "BLOCKED"


@pytest.mark.parametrize("prefix", ["", "market-data/", CHECKPOINT_PREFIX + "child/"])
def test_overlapping_expiration_rule_blocks(prefix):
    evidence = lifecycle_fixture()
    evidence["expirationRules"].append({"prefix": prefix, "expirationDays": 1})
    assert "lifecycle_overlapping_or_missing_rule" in checkpoint_lifecycle_preflight(evidence, now=NOW)["blockers"]


@pytest.mark.parametrize("value", [True, -1, 0, 91, None])
def test_retention_is_finite_integer(value):
    evidence = lifecycle_fixture()
    evidence["expirationRules"][0]["expirationDays"] = value
    assert "lifecycle_retention_invalid" in checkpoint_lifecycle_preflight(evidence, now=NOW)["blockers"]


@pytest.mark.parametrize("section,field,value", [
    ("referenceAudit", "atRiskCount", True), ("referenceAudit", "atRiskCount", 1),
    ("referenceAudit", "complete", False),
    ("referenceAudit", "observedAt", "2026-10-01T00:00:00Z"),
    ("restoreEvidence", "verified", False), ("restoreEvidence", "prefix", "market-data/"),
    ("restoreEvidence", "observedAt", None),
])
def test_reference_and_restore_evidence_fail_closed(section, field, value):
    evidence = lifecycle_fixture()
    evidence[section][field] = value
    assert checkpoint_lifecycle_preflight(evidence, now=NOW)["status"] == "BLOCKED"


def test_unbounded_rules_and_unknown_evidence_block():
    evidence = lifecycle_fixture()
    evidence["expirationRules"] *= 33
    assert "lifecycle_rules_unknown_or_unbounded" in checkpoint_lifecycle_preflight(evidence, now=NOW)["blockers"]
    assert "lifecycle_evidence_unknown" in checkpoint_lifecycle_preflight(None, now=NOW)["blockers"]
    with pytest.raises(ValueError, match="timezone"):
        checkpoint_lifecycle_preflight(evidence, now=NOW.replace(tzinfo=None))
