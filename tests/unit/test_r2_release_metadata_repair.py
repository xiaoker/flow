from __future__ import annotations

import hashlib
import json

import pytest

from scripts.repair_r2_release_metadata import (
    APPLY_CONFIRMATION,
    MAX_OBJECTS,
    build_candidates,
    build_report,
    canonical_plan_hash,
    select_candidates,
)


AS_OF = "2026-08-06"
RELEASE_ID = "release:2026-08-06:0123456789abcdef"


def _row(*, names=("overview",), status="SUPERSEDED", as_of=AS_OF, release_id=RELEASE_ID):
    artifacts = {}
    for index, name in enumerate(names):
        body = f"{name}:{index}".encode()
        artifacts[name] = {
            "key": f"market-data/releases/{as_of}/{release_id}/{name}.json",
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }
    return {
        "as_of": as_of,
        "release_id": release_id,
        "status": status,
        "manifest_json": json.dumps(
            {"schemaVersion": 2, "asOf": as_of, "releaseId": release_id, "artifacts": artifacts},
            separators=(",", ":"),
        ),
    }


def test_candidates_are_sorted_and_deduplicated_by_exact_key() -> None:
    row = _row(names=("overview", "margin"))
    candidates = build_candidates([row, row], start=AS_OF, end=AS_OF)
    assert [item.artifact_name for item in candidates] == ["margin", "overview"]
    assert len({item.key for item in candidates}) == 2


def test_conflicting_descriptors_fail_closed() -> None:
    first = _row()
    second = _row()
    manifest = json.loads(second["manifest_json"])
    manifest["artifacts"]["overview"]["sha256"] = "f" * 64
    second["manifest_json"] = json.dumps(manifest)
    with pytest.raises(ValueError, match="artifact_descriptor_conflict"):
        build_candidates([first, second], start=AS_OF, end=AS_OF)


def test_apply_requires_complete_selection_and_respects_hard_ceiling() -> None:
    candidates = build_candidates([_row(names=("overview", "margin"))], start=AS_OF, end=AS_OF)
    with pytest.raises(ValueError, match="apply_selection_incomplete"):
        select_candidates(candidates, max_objects=1, max_bytes=MAX_OBJECTS, mode="apply")
    selected, truncated = select_candidates(candidates, max_objects=MAX_OBJECTS, max_bytes=500_000_000, mode="apply")
    assert not truncated
    assert len(selected) == 2


def test_plan_hash_is_stable_for_same_fixture_and_changes_with_mode() -> None:
    candidates = build_candidates([_row(names=("overview", "margin"))], start=AS_OF, end=AS_OF)
    first = canonical_plan_hash(candidates, start=AS_OF, end=AS_OF, mode="dry-run")
    second = canonical_plan_hash(list(reversed(candidates)), start=AS_OF, end=AS_OF, mode="dry-run")
    assert first == second
    assert first != canonical_plan_hash(candidates, start=AS_OF, end=AS_OF, mode="apply")


def test_report_preserves_unknown_budget_and_never_calls_it_zero() -> None:
    payload = [{"results": [_row()], "meta": {"rows_read": 1}}]
    report, _ = build_report(
        payload=payload,
        start=AS_OF,
        end=AS_OF,
        mode="dry-run",
        max_objects=1,
        max_bytes=10_000_000,
    )
    assert report["status"] == "INCOMPLETE_BUDGET_UNKNOWN"
    assert report["d1RowsRead"] == 1
    assert report["d1RowsWritten"] is None


def test_report_blocks_a_degraded_d1_query_before_any_repair_request() -> None:
    payload = [{"results": [_row()], "meta": {"rows_read": 500_001, "rows_written": 0}}]
    report, selected = build_report(
        payload=payload,
        start=AS_OF,
        end=AS_OF,
        mode="dry-run",
        max_objects=1,
        max_bytes=10_000_000,
    )
    assert report["status"] == "BLOCKED_D1_READ_CAP"
    assert len(selected) == 1


def test_confirmation_constant_is_not_a_secret() -> None:
    assert APPLY_CONFIRMATION.startswith("R2_METADATA_REPAIR_APPLY_")
