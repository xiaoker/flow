from __future__ import annotations

from pathlib import Path
import re
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_candidate_workflow_is_manual_default_safe_and_uses_exact_sha() -> None:
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
    assert "inputs.execution_mode == 'candidate' && secrets.FLOW_INGEST_API_KEY" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "npx wrangler" not in workflow
    assert "activeReleaseChanged == false" in workflow


def test_candidate_workflow_uploads_only_bounded_fixture_evidence() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text(encoding="utf-8")
    assert "retention-days: 7" in workflow
    assert "${{ runner.temp }}/flow-pipeline-candidate/summary.json" in workflow
    assert "${{ runner.temp }}/flow-pipeline-candidate/result.json" in workflow
    assert "flow-pipeline-candidate/**" not in workflow
    assert "productionEntryEnabled == false" in workflow


def test_candidate_workflow_dry_run_is_explicit_and_non_authoritative() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    assert "default: fixture" in workflow
    assert "- dry-run" in workflow
    assert '--execution-mode "$EXECUTION_MODE"' in workflow
    assert 'case "$EXECUTION_MODE" in fixture|dry-run|candidate)' in workflow
    assert "DECLARED_INPUT_CHECKED" in workflow
    assert "serverAuthorityVerified == false" in workflow
    assert 'test ! -e "$RUNNER_TEMP/flow-pipeline-candidate/state"' in workflow
    assert "--source-run-id \"$SOURCE_RUN_ID\"" in workflow
    assert "--artifact-id \"$SOURCE_ARTIFACT_ID\"" in workflow
    assert "timeout-minutes: 10" in workflow
    assert "--no-deps -r config/pipeline-python-runtime.lock" in workflow
    assert "--dependency-lock config/pipeline-python-runtime.lock" in workflow
    assert "inputs.execution_mode == 'dry-run' && inputs.request_json != ''" in workflow
    assert 'request="$RUNNER_TEMP/flow-pipeline-source/request.json"' in workflow
    assert '--request "$request"' in workflow
    assert '"${extra[@]}"' in workflow
    assert "flow-pipeline-source/**" not in workflow


def test_manual_entry_shell_blocks_parse_and_do_not_interpolate_inputs() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    blocks = re.findall(r"(?m)^        run: \|\n((?:          [^\n]*\n|\n)+)", workflow)
    # Include the explicit preparation guard and the one-shot authority entry;
    # every shell block still receives syntax and input-boundary validation.
    assert len(blocks) == 6
    for block in blocks:
        script = textwrap.dedent(block)
        assert "${{" not in script  # Untrusted declarations cross environment only.
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_archive_verification_mode_is_main_only_exact_bounded_and_cannot_execute_candidate() -> None:
    workflow = (ROOT / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    assert "- archive-verify" in workflow
    assert "inputs.execution_mode != 'archive-verify') || github.ref == 'refs/heads/main'" in workflow
    assert "if: ${{ inputs.execution_mode == 'archive-verify' }}" in workflow
    assert "inputs.preparation_json == '' && inputs.execution_mode != 'archive-verify'" in workflow
    assert "if: ${{ inputs.execution_mode == 'candidate' }}" in workflow
    assert "if: ${{ inputs.execution_mode == 'candidate' && inputs.preparation_json != '' }}" in workflow
    start = workflow.index("- name: Verify exact Phase 0 archive objects without execution authority")
    end = workflow.index("- name: Prepare one exact archived source", start)
    block = workflow[start:end]
    assert "[[ \"$ARCHIVE_CURRENT_RUN_ID\" =~ ^[0-9]{1,12}$ ]]" in block
    assert "[[ \"$ARCHIVE_PREVIOUS_RUN_ID\" =~ ^[0-9]{1,12}$ ]]" in block
    assert "test \"$ARCHIVE_CURRENT_RUN_ID\" != \"$ARCHIVE_PREVIOUS_RUN_ID\"" in block
    assert ".size_in_bytes <= 40000000" in block
    assert "--previous-history-bundle" in block and "--verify-live-archive" in block
    assert ".archiveReadCount > 0 and .archiveReadCount <= 96" in block
    assert "gh run download" in block and "gh api" in block
    assert "wrangler" not in block and "workflow_dispatch" not in block
    assert "pipeline-window" not in block and "candidate-release" not in block
    assert "archive-verified-recipe.json" in workflow
