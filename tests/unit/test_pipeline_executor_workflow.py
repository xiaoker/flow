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
    assert len(blocks) == 3
    for block in blocks:
        script = textwrap.dedent(block)
        assert "${{" not in script  # Untrusted declarations cross environment only.
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
