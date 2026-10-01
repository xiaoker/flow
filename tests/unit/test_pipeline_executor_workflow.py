from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_candidate_workflow_is_manual_fixture_only_and_uses_exact_sha() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "pull_request:" not in workflow
    assert "schedule:" not in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in workflow
    assert "trading_desk.cli pipeline" in workflow
    assert "--dependency-lock web/package-lock.json" in workflow
    assert "--producer-build-sha \"$GITHUB_SHA\"" in workflow
    assert "FLOW_INGEST_API_KEY" not in workflow
    assert "npx wrangler" not in workflow
    assert "activeReleaseChanged == false" in workflow


def test_candidate_workflow_uploads_only_bounded_fixture_evidence() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text(encoding="utf-8")
    assert "retention-days: 7" in workflow
    assert "${{ runner.temp }}/flow-pipeline-candidate/**" in workflow
    assert "productionEntryEnabled == false" in workflow
