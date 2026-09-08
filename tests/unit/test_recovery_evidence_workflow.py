from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/controlled-recovery-inventory.yml"


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?m)^{re.escape(heading)}:\s*$", text)
    assert match, f"missing workflow section {heading}"
    tail = text[match.end() :]
    next_heading = re.search(r"(?m)^\S[^:]*:\s*$", tail)
    return tail[: next_heading.start()] if next_heading else tail


def test_workflow_is_manual_minimum_permission_and_single_concurrency_group() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s+workflow_dispatch:\s*$", text)
    assert not re.search(r"(?m)^\s+schedule:\s*$", text)
    permissions = _section(text, "permissions")
    assert re.search(r"(?m)^\s+contents:\s+read\s*$", permissions)
    concurrency = _section(text, "concurrency")
    assert "group: controlled-recovery-inventory" in concurrency
    assert "cancel-in-progress: false" in concurrency


def test_workflow_inputs_are_fixed_and_preflight_precedes_export() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'default: "2024-08-01"' in text
    assert 'default: "2026-09-03"' in text
    assert "priority_dates:" in text
    preflight_at = text.index("Preflight scope and read-only permissions")
    exporter_at = text.index("Export evidence and run offline auditor twice")
    assert preflight_at < exporter_at
    assert "--phase R0-D" in text
    assert "--output-dir \"$RUNNER_TEMP/controlled-recovery\"" in text
    assert "Prepare optional ephemeral official usage export" in text
    assert 'if [ -n "${FLOW_OFFICIAL_USAGE_EXPORT_JSON:-}" ]; then' in text
    assert 'if [ -s "$RUNNER_TEMP/official-usage-export.json" ]; then' in text
    assert 'usage_args+=(--usage-file "$RUNNER_TEMP/official-usage-export.json")' in text
    assert '"${usage_args[@]}"' in text


def test_workflow_timeout_matches_the_checked_in_local_budget_envelope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    config = json.loads((ROOT / "config/resource-budget.json").read_text(encoding="utf-8"))
    timeout = int(re.search(r"(?m)^\s+timeout-minutes:\s+(\d+)\s*$", text).group(1))
    assert timeout == config["readOnlyInventoryBudget"]["actionsMinutesUpperBound"]


def test_workflow_has_no_collection_publish_dispatch_or_secret_artifact_paths() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text  # manual trigger, not a dispatch API call
    for forbidden in ("market-data-collect", "market-data-publish", "Watchdog", "Collector", "Publish", "wrangler", "PIPELINE_RECOVERY_ENABLED"):
        assert forbidden not in text
    assert "FLOW_INGEST_API_KEY: ${{ secrets.FLOW_INGEST_API_KEY }}" in text
    assert "FLOW_CF_ACCESS_CLIENT_SECRET: ${{ secrets.FLOW_CF_ACCESS_CLIENT_SECRET }}" in text
    assert "FLOW_OFFICIAL_USAGE_EXPORT_JSON: ${{ secrets.FLOW_OFFICIAL_USAGE_EXPORT_JSON }}" in text
    assert "official_usage_export_missing" not in text
    assert "Remove ephemeral official usage export" in text
    assert "retention-days: 14" in text
    assert "if: ${{ always() }}" in text
