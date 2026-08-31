from pathlib import Path
import os
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY_ROOT / ".github/workflows"
LEGACY_WORKFLOW = WORKFLOWS / "daily-data.yml"
COLLECT_WORKFLOW = WORKFLOWS / "market-data-collect.yml"
PUBLISH_WORKFLOW = WORKFLOWS / "market-data-publish.yml"
WATCHDOG_WORKFLOW = WORKFLOWS / "market-data-watchdog.yml"
PCF_WORKFLOW = WORKFLOWS / "pcf-v2-capture.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"


def test_market_data_split_has_watchdog_and_cloudflare_dispatch() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    watchdog = WATCHDOG_WORKFLOW.read_text(encoding="utf-8")
    legacy = LEGACY_WORKFLOW.read_text(encoding="utf-8")
    wrangler = (REPOSITORY_ROOT / "web/wrangler.jsonc").read_text(encoding="utf-8")
    worker = (REPOSITORY_ROOT / "web/worker/index.ts").read_text(encoding="utf-8")

    assert "schedule:" not in workflow
    assert "schedule:" in watchdog
    assert '- cron: "45 10 * * 1-5"' in watchdog
    assert '- cron: "0 0 * * 1-5"' in watchdog
    assert '- cron: "55 0 * * 1-5"' in watchdog
    assert "workflow_dispatch:" in workflow
    assert "workflow_dispatch:" in legacy
    assert "cancel-in-progress: false" in workflow
    assert "cancel-in-progress: false" in watchdog
    assert '"30 10 * * mon-fri"' in wrangler
    assert '"45 23 * * sun-thu"' in wrangler
    assert '"45 0 * * mon-fri"' in wrangler
    assert '"15 11 * * mon-fri"' in wrangler
    assert 'cron.startsWith("15 1")' in worker
    assert "GITHUB_ACTIONS_TOKEN" in worker
    assert "market-data-collect.yml/dispatches" in worker
    assert "market-data-publish.yml/dispatches" in workflow
    assert "market-data-collect.yml/dispatches" in legacy
    assert 'ref: "main"' in worker
    assert 'collection_window: window' in worker
    assert 'run_key: runKey' in worker
    assert "response.status === 200 || response.status === 204" in worker
    assert 'console.error("Daily collection dispatch failed"' in worker
    assert "DISPATCH_RETRY_DELAYS_MS = [0, 1_000, 3_000]" in worker
    assert "LATE_SCHEDULE_THRESHOLD_MS = 5 * 60 * 1_000" in worker
    assert "isRetryableDispatchStatus" in worker
    assert "scheduleLagMs" in worker
    assert 'console.warn("GitHub workflow dispatch will be retried"' in worker


def test_pr2_moves_full_schedules_to_a_lightweight_watchdog() -> None:
    """The heavy workflow is dispatch-only; the watchdog owns failover schedules."""

    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    watchdog = WATCHDOG_WORKFLOW.read_text(encoding="utf-8")
    legacy = LEGACY_WORKFLOW.read_text(encoding="utf-8")
    wrangler = (REPOSITORY_ROOT / "web/wrangler.jsonc").read_text(encoding="utf-8")
    worker = (REPOSITORY_ROOT / "web/worker/index.ts").read_text(encoding="utf-8")

    github_schedules = [line for line in watchdog.splitlines() if "- cron:" in line]
    cloudflare_schedules = [
        line
        for line in wrangler.splitlines()
        if "mon-fri" in line or "sun-thu" in line
    ]
    assert len(github_schedules) == 5
    assert len(cloudflare_schedules) == 5
    assert "group: market-data-collect" in workflow
    assert "group: market-data-watchdog" in watchdog
    assert "cancel-in-progress: false" in workflow
    assert "schedule:" not in workflow
    assert "schedule:" not in legacy
    assert "concurrency" in workflow


def test_pr2_collect_hands_off_an_immutable_source_artifact() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")

    assert "delegate_publish:" in workflow
    assert "default: true" in workflow
    assert "Upload immutable collection source bundle" in workflow
    assert "market-data-collection-source-${{ github.run_id }}" in workflow
    assert "Dispatch resumable market-data publish workflow" in workflow
    assert "market-data-publish.yml/dispatches" in workflow
    assert "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f" in workflow
    assert "needs.pipeline_window_gate.outputs.delegate_publish == 'true'" in workflow
    assert "needs.pipeline_window_gate.outputs.delegate_publish != 'true'" in workflow
    assert "Record collection resource budget" in workflow
    assert "report_stage_budget.py" in workflow
    assert "python -m pytest\n" not in workflow
    assert "python -m pytest -q" in workflow


def test_dispatchers_accept_github_200_and_204_and_fail_closed_on_incomplete_artifact() -> None:
    collect = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    watchdog = WATCHDOG_WORKFLOW.read_text(encoding="utf-8")
    legacy = LEGACY_WORKFLOW.read_text(encoding="utf-8")

    assert collect.count('[[ "$STATUS_CODE" != "200" && "$STATUS_CODE" != "204" ]]') == 2
    assert '[[ "$DISPATCH_CODE" != "200" && "$DISPATCH_CODE" != "204" ]]' in watchdog
    assert '[[ "$STATUS_CODE" != "200" && "$STATUS_CODE" != "204" ]]' in legacy
    assert collect.count("workflow_run_id") >= 2
    assert "workflow_run_id" in watchdog
    assert "workflow_run_id" in legacy
    assert "if: ${{ success() && steps.collection.outcome == 'success'" in collect


def test_stage_budget_cli_runs_from_repository_root(tmp_path: Path) -> None:
    output = tmp_path / "stage-budget.json"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/report_stage_budget.py",
            "--stage",
            "coreCollection",
            "--output",
            str(output),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()


def test_pr2_publish_is_dispatch_only_and_uses_targeted_smoke() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "run-id: ${{ inputs.source_run_id }}" in workflow
    assert "tests/unit/test_publish_read_models.py" in workflow
    assert "tests/unit/test_read_models.py" in workflow
    assert "python -m pytest -q" in workflow
    assert "Mark pipeline window terminal status" in workflow
    assert "report_stage_budget.py" in workflow
    assert "market-data-collect.yml/dispatches" not in workflow
    assert "leaseExpiresAt" in workflow
    assert "Publish lease has expired" in workflow


def test_pr2_watchdog_is_lightweight_and_dispatches_only_after_claim() -> None:
    workflow = WATCHDOG_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("- cron:") == 5
    assert "pipeline-window-status.json" in workflow
    assert "pipeline-window-claim-request.json" in workflow
    assert "market-data-collect.yml/dispatches" in workflow
    assert "delegate_publish:\"true\"" in workflow
    assert "actions/checkout" not in workflow
    assert "setup-python" not in workflow
    assert "npm ci" not in workflow
    assert "pytest" not in workflow
    assert "timeout-minutes: 2" in workflow
    assert "fail closed" in workflow
    assert "actionsRunAttempt" in workflow
    assert "r2Bytes:0" in workflow


def test_pr2_pcf_workflow_is_independent_and_checkpoint_resumable() -> None:
    workflow = PCF_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "schedule:" not in workflow
    assert "Build independent exact-date PCF v2 plan" in workflow
    assert "--official-live" in workflow
    assert "Capture exact PCF v2 items with D1 checkpoints" in workflow
    assert "capture_sse_pcf_v2" in workflow
    assert "Finalize independent PCF v2 canonical checkpoint" in workflow
    assert "finalize_sse_pcf_v2" in workflow
    assert "summarize_pcf_v2_history" in workflow
    assert "Fail PCF workflow on unresolved checkpoint state" in workflow
    assert "penetration capability remains unchanged" in workflow
    assert "/api/v1/admin/pcf-capture" not in workflow
    assert "market-data-collect.yml" not in workflow
    assert "market-data-publish.yml" not in workflow
    assert "report_stage_budget.py" in workflow


def test_pr2_ci_covers_python_and_web_without_schedules() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "web-ci:" in workflow
    assert "npm ci" in workflow
    assert "npm test" in workflow
    assert "npm run lint" in workflow
    assert "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38" in workflow


def test_pr1_pipeline_window_gate_precedes_heavy_collection() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    worker = (REPOSITORY_ROOT / "web/worker/index.ts").read_text(encoding="utf-8")
    claim_route = (REPOSITORY_ROOT / "web/app/api/v1/admin/pipeline-window/claim/route.ts").read_text(encoding="utf-8")
    status_route = (REPOSITORY_ROOT / "web/app/api/v1/admin/pipeline-window/status/route.ts").read_text(encoding="utf-8")
    migration = (REPOSITORY_ROOT / "web/drizzle/0010_fair_jasper_sitwell.sql").read_text(encoding="utf-8")
    pipeline_window = (REPOSITORY_ROOT / "web/app/lib/pipeline-window.ts").read_text(encoding="utf-8")

    assert "pipeline_window_gate:" in workflow
    assert "needs: pipeline_window_gate" in workflow
    assert "if: ${{ needs.pipeline_window_gate.outputs.should_run == 'true' }}" in workflow
    assert "FLOW_PIPELINE_WINDOW_URL/status?runKey=" in workflow
    assert "FLOW_PIPELINE_WINDOW_URL/claim" in workflow
    assert "Mark pipeline window running" in workflow
    assert "Mark pipeline window terminal status" in workflow
    assert "setup-python" in workflow
    assert workflow.index("Claim or skip pipeline window") < workflow.index("setup-python")
    assert "run_key:" in workflow
    assert "window_name:" in workflow
    assert "target_date:" in workflow
    assert "lease_owner:" in workflow
    assert "Concurrency is not cross-scheduler deduplication." in workflow
    assert "pipeline_window is the idempotency boundary" in workflow

    assert "PIPELINE_WINDOW_GATE_ENABLED" in worker
    assert "claimPipelineWindow" in worker
    assert "buildPipelineRunKey" in worker
    assert "decision !== \"acquired\" && claim.decision !== \"takeover\"" in worker
    assert 'run_key: runKey' in worker
    assert 'window_name: window' in worker
    assert 'target_date: windowDate' in worker
    assert 'lease_owner: leaseOwner' in worker
    assert "pipeline_window" in migration
    assert "CREATE INDEX `pipeline_window_status_lease_idx`" in migration
    assert "ALTER TABLE" not in migration
    assert "ON CONFLICT(run_key) DO UPDATE" in pipeline_window
    assert "WHERE pipeline_window.status" in pipeline_window
    assert "SELECT-then-UPDATE" in pipeline_window
    assert "pipeline_window_not_found" in status_route
    assert "secureEqual" in claim_route


def test_pcf_capture_control_plane_is_additive_and_worker_upload_is_streaming() -> None:
    capture = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/route.ts").read_text(encoding="utf-8")
    upload = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/object/route.ts").read_text(encoding="utf-8")
    schema = (REPOSITORY_ROOT / "web/db/schema.ts").read_text(encoding="utf-8")

    assert "pcf_capture_job" in schema
    assert "pcf_capture_item" in schema
    assert "history_artifact_pointer" in schema
    assert "BLOCKED_UNIVERSE_ANOMALY" in capture
    assert "ITEM_BATCH_SIZE = 25" in capture
    assert "request.body" in upload
    assert "arrayBuffer()" not in upload
    assert "text()" not in upload
    assert "RAW_BUCKET.put(objectKey, request.body" in upload
    assert "sha256: objectSha256" in upload
    assert "!contentLengthHeader" in upload
    assert "items: items.results" in capture
    history = (REPOSITORY_ROOT / "web/app/api/v1/admin/history-artifact/route.ts").read_text(encoding="utf-8")
    assert "history_artifact_pointer" in history
    assert "pointerFor(runtime" in history
    assert "R2_BUCKET.list" not in history
    artifact = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/artifact/route.ts").read_text(encoding="utf-8")
    finalize = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/finalize/route.ts").read_text(encoding="utf-8")
    assert "request.body" in artifact
    assert "pcf_artifact_not_verified" in finalize
    assert "pcf_manifest_not_verified" in finalize
    assert "history_artifact_pointer" in finalize
    lease = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/lease/route.ts").read_text(encoding="utf-8")
    assert "lease_expires_at" in lease
    assert "pcf_capture_lease_unavailable" in lease


def test_github_actions_use_node24_compatible_pinned_revisions() -> None:
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPOSITORY_ROOT / ".github/workflows").glob("*.yml")
    )
    assert "actions/checkout@v4" not in workflow_text
    assert "actions/setup-python@v5" not in workflow_text
    assert "actions/upload-artifact@v4" not in workflow_text
    assert "actions/setup-node@v4" not in workflow_text
    assert "actions/checkout@8e8c483db84b4bee98b60c0593521ed34d9990e8" in workflow_text
    assert "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c" in workflow_text
    assert "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f" in workflow_text
    assert "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38" in workflow_text


def test_deploy_canary_waits_for_the_exact_worker_build_after_rollout() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-worker.yml").read_text(
        encoding="utf-8"
    )

    assert "Set build metadata" in workflow
    assert "FLOW_BUILD_SHA=${GITHUB_SHA}" in workflow
    assert workflow.index("Set build metadata") < workflow.index(
        "Run Web lint and production contract tests"
    )
    assert workflow.index("Set build metadata") < workflow.index(
        "Deploy Worker and static assets"
    )
    assert "for attempt in $(seq 1 20)" in workflow
    assert "build_check=$GITHUB_SHA&attempt=$attempt" in workflow
    assert '"Cache-Control: no-cache"' in workflow
    assert "Verified deployed Worker build identity" in workflow
    assert "did not expose build SHA" in workflow
    assert "grep -FiE" not in workflow


def test_daily_collection_retries_network_failures_and_archives_pending_sources() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")

    assert "MAX_ATTEMPTS=3" in workflow
    assert "PCF_MAX_CODES=16" in workflow
    assert "--pcf-max-codes \"$PCF_MAX_CODES\"" in workflow
    assert "SOURCE_READINESS_ATTEMPTS=18" not in workflow
    assert "sleep 120" not in workflow
    assert "id: collection" in workflow
    assert 'echo "pending=false" >> "$GITHUB_OUTPUT"' in workflow
    assert 'echo "pending=true" >> "$GITHUB_OUTPUT"' in workflow
    assert 'source_pending="${{ steps.collection.outputs.pending }}"' in workflow
    assert "Official source date is not ready" in workflow
    assert "Collection window pending" in workflow
    assert 'timeliness.requested_date_coverage' in workflow
    assert "pending_domains" in workflow
    assert '.status == "archived_pending"' in workflow
    assert '.error_type == "ConnectionError"' in workflow
    assert '.error_type == "HTTPError"' in workflow
    assert workflow.index("Build dashboard snapshot") < workflow.index(
        "Build verified archive and publish bundle"
    )
    assert workflow.index("Archive raw evidence and publish normalized data") < workflow.index(
        "Verify deployed dashboard read-model capabilities"
    )
    assert workflow.index("Archive raw evidence and publish normalized data") < workflow.index(
        "Build ETF-to-stock daily partitions from archived R2 inputs"
    )
    assert workflow.index("Verify deployed dashboard read-model capabilities") < workflow.index(
        "Build immutable dashboard and penetration read models offline"
    )
    assert workflow.index("Stage immutable dashboard read-model candidate") < workflow.index(
        "Verify staged dashboard read-model candidate"
    )
    assert "build_stock_pressure_state.py" in workflow
    assert "--daily-partitions stock-flow-history.json" in workflow
    assert "Build ETF-to-stock daily partitions from archived R2 inputs" in workflow
    assert "fetch_stock_flow_history_inputs.py" in workflow
    assert "build_stock_flow_history.py" in workflow
    assert "publish_stock_flow_history.py" in workflow
    assert "--lineage-report stock-flow-fetch-report.json" in workflow
    assert "--inventory pcf-inventory.json" in workflow
    assert "Build immutable dashboard and penetration read models offline" in workflow
    assert "--require-materialized" in workflow
    assert "--read-model-dir read-models-full" in workflow
    assert "--slot dashboard" in workflow
    assert "--slot etf_penetration" in workflow
    assert "check_release_capabilities.py" in workflow
    assert "verify_published_release.py" in workflow
    assert "--repeat 100" not in workflow
    assert "--result-output dashboard-read-model-publish.json" in workflow
    assert "'.status == \"staged_candidate\"'" in workflow
    assert "Activate canary-verified dashboard candidate" in workflow
    assert '"Snapshot is pending source confirmation' in workflow
    assert "Fail loudly on unresolved source errors" in workflow
    assert "Collection archived partial evidence but still has unresolved source errors." in workflow


def test_pcf_shadow_parity_is_explicitly_opt_in_and_never_published() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    script = (REPOSITORY_ROOT / "scripts/run_pcf_shadow_parity.py").read_text(encoding="utf-8")
    summary = (REPOSITORY_ROOT / "scripts/summarize_pcf_shadow.py").read_text(encoding="utf-8")
    parity = (REPOSITORY_ROOT / "src/trading_desk/pcf_parity.py").read_text(encoding="utf-8")

    assert "run_pcf_shadow:" in workflow
    assert "default: false" in workflow
    assert "inputs.run_pcf_shadow == true" in workflow
    assert "Run opt-in PCF shadow parity (never published)" in workflow
    assert "reports/pcf-shadow/*.json" in workflow
    assert "--data-root \".shadow/pcf-v2/${DATES[2]}\"" in workflow
    assert "productionPublishCalls" in parity
    assert "/api/v1/admin/import" not in script
    assert "--required-days" in summary
    assert "default=5" in summary


def test_pcf_v2_exact_capture_is_owned_by_independent_workflow() -> None:
    collect_workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")
    pcf_workflow = PCF_WORKFLOW.read_text(encoding="utf-8")
    capture = (REPOSITORY_ROOT / "scripts/capture_sse_pcf_v2.py").read_text(encoding="utf-8")
    finalize = (REPOSITORY_ROOT / "scripts/finalize_sse_pcf_v2.py").read_text(encoding="utf-8")
    object_route = (REPOSITORY_ROOT / "web/app/api/v1/admin/pcf-capture/object/route.ts").read_text(encoding="utf-8")

    assert "Build automatic PCF v2 exact-date capture plan" not in collect_workflow
    assert "Capture resumable PCF v2 exact-date XML" not in collect_workflow
    assert "Finalize complete PCF v2 canonical shadow" not in collect_workflow
    assert "Summarize PCF v2 five-day shadow gate" not in collect_workflow
    assert "Dispatch independent PCF v2 checkpoint workflow" in collect_workflow
    assert "steps.dates.outputs.latest" in collect_workflow
    pcf_dispatch = collect_workflow[collect_workflow.index("Dispatch independent PCF v2 checkpoint workflow") :]
    assert "continue-on-error: true" in pcf_dispatch
    assert "pcf-dispatch-evidence.json" in collect_workflow
    assert "Build independent exact-date PCF v2 plan" in pcf_workflow
    assert "Capture exact PCF v2 items with D1 checkpoints" in pcf_workflow
    assert "Finalize independent PCF v2 canonical checkpoint" in pcf_workflow
    assert "--official-live" in pcf_workflow
    assert "python -m scripts.capture_sse_pcf_v2" in pcf_workflow
    assert "python -m scripts.finalize_sse_pcf_v2" in pcf_workflow
    assert "python -m scripts.summarize_pcf_v2_history" in pcf_workflow
    assert "reports/pcf-v2/checkpoint-summary.json" in pcf_workflow
    assert '--arg requiredDays "1"' in collect_workflow
    assert "DATE_MISMATCH" in capture
    assert "market-data/pcf-v2/degraded" not in finalize
    assert "productionPublishCalls" in finalize
    assert "export async function GET" in object_route
    assert "pcf_exact_object_lineage_mismatch" in object_route


def test_production_canary_isolated_from_daily_collection() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/production-canary.yml"
    ).read_text(encoding="utf-8")

    assert '- cron: "30 2 * * 1"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "--repeat 100" in workflow
    assert "--slot dashboard" in workflow
    assert "Resolve active release" in workflow
    assert "buildSha" in workflow
    assert "cancel-in-progress: false" in workflow


def test_daily_collection_does_not_publish_empty_stock_flow_over_a_complete_index() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")

    assert "Check ETF stock-flow read-model readiness" in workflow
    assert "etfStockFlow.available == true" in workflow
    assert "((.etfStockFlow.coverage.stockCount // 0) > 0)" in workflow
    assert 'steps.stock_flow.outputs.ready' in workflow
    readiness_index = workflow.index("Check ETF stock-flow read-model readiness")
    build_index = workflow.index("Build immutable dashboard and penetration read models offline")
    stage_index = workflow.index("Stage immutable dashboard read-model candidate")
    assert readiness_index < build_index < stage_index


def test_stock_flow_history_is_bounded_and_current_partition_is_required() -> None:
    workflow = COLLECT_WORKFLOW.read_text(encoding="utf-8")

    assert "| .[-22:][]" in workflow
    assert "--require-current" in workflow
    assert "stock-flow-history.json" in workflow
    assert "availableDates | index($asOf) != null" in workflow


def test_sse_relay_has_an_independent_scheduled_smoke_check() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/access-smoke.yml"
    ).read_text(encoding="utf-8")

    assert '- cron: "30 0 * * 1"' in workflow
    assert "SOURCE_PROXY_URL: https://flow.xiaoker.com/api/v1/admin/source-proxy" in workflow
    assert "Authorization: Bearer $INGEST_API_KEY" in workflow
    assert "sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L" in workflow
    assert "Authenticated, allowlisted SSE fallback transport is healthy." in workflow


def test_r2_recovery_drill_is_read_only_and_preserves_evidence() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/r2-recovery-drill.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "historical_snapshot" in workflow
    assert "history-snapshot" in workflow
    assert "X-Flow-History-Snapshot-Sha256" in workflow
    assert "fetch_stock_flow_history_inputs.py" in workflow
    assert "r2_recovery_drill.py" in workflow
    assert "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f" in workflow
    assert "r2 object put" not in workflow
    assert "d1 execute flow --remote --json" in workflow
    assert "--apply" not in workflow


def test_stock_flow_inventory_is_metadata_only_and_does_not_publish() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/stock-flow-inventory.yml").read_text(encoding="utf-8")
    script = (REPOSITORY_ROOT / "scripts/inventory_stock_flow_inputs.py").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "market_history_daily" in workflow
    assert "inventory_stock_flow_inputs.py" in workflow
    assert "publish_stock_flow_history.py" not in workflow
    assert "history-import" not in workflow
    assert "metadata_only" in script
    assert "stream=True" in script
    assert "response.close()" in script
