from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_daily_collection_has_github_failover_and_cloudflare_dispatch() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")
    wrangler = (REPOSITORY_ROOT / "web/wrangler.jsonc").read_text(encoding="utf-8")
    worker = (REPOSITORY_ROOT / "web/worker/index.ts").read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert '- cron: "45 10 * * 1-5"' in workflow
    assert '- cron: "0 0 * * 1-5"' in workflow
    assert '- cron: "55 0 * * 1-5"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert '"crons": ["30 10 * * mon-fri", "45 23 * * sun-thu", "45 0 * * mon-fri"]' in wrangler
    assert "GITHUB_ACTIONS_TOKEN" in worker
    assert "daily-data.yml/dispatches" in worker
    assert 'ref: "main"' in worker
    assert 'inputs: { collection_window: collectionWindowForCron(cron) }' in worker
    assert "response.status !== 200 && response.status !== 204" in worker
    assert 'console.error("Daily collection dispatch failed"' in worker


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
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")

    assert "MAX_ATTEMPTS=3" in workflow
    assert "phase0_gate_status=pending" in workflow
    assert "Source-not-ready is pending" in workflow
    assert "SOURCE_READINESS_ATTEMPTS" not in workflow
    assert "sleep 120" not in workflow
    assert "pending_domains" in workflow
    assert '.status == "archived_pending"' in workflow
    assert '.error_type == "ConnectionError"' in workflow
    assert '.error_type == "HTTPError"' in workflow
    assert workflow.index("Build dashboard snapshot") < workflow.index(
        "Build verified archive and publish bundle"
    )
    assert workflow.index("Archive raw evidence and publish normalized data") < workflow.index(
        "Verify deployed read-model capabilities before building release"
    )
    assert workflow.index("Archive raw evidence and publish normalized data") < workflow.index(
        "Build ETF-to-stock daily partitions from archived R2 inputs"
    )
    assert workflow.index("Verify deployed read-model capabilities before building release") < workflow.index(
        "Build immutable read models offline"
    )
    assert workflow.index("Publish immutable read models") < workflow.index(
        "Verify published read-model release"
    )
    assert "build_stock_pressure_state.py" in workflow
    assert "--daily-partitions stock-flow-history.json" in workflow
    assert "Build ETF-to-stock daily partitions from archived R2 inputs" in workflow
    assert "fetch_stock_flow_history_inputs.py" in workflow
    assert "build_stock_flow_history.py" in workflow
    assert "publish_stock_flow_history.py" in workflow
    assert "--lineage-report stock-flow-fetch-report.json" in workflow
    assert "--inventory pcf-inventory.json" in workflow
    assert "Build immutable read models offline" in workflow
    assert "--require-materialized" in workflow
    assert "--read-model-dir read-models" in workflow
    assert "check_release_capabilities.py" in workflow
    assert "verify_published_release.py" in workflow
    assert "--repeat 100" in workflow
    assert "--result-output read-model-publish.json" in workflow
    assert "'.status == \"activated\"'" in workflow
    assert '"Snapshot is pending source confirmation' in workflow
    assert "Fail loudly on unresolved source errors" in workflow
    assert "Collection archived partial evidence but still has unresolved source errors." in workflow


def test_daily_collection_does_not_publish_empty_stock_flow_over_a_complete_index() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/daily-data.yml").read_text(encoding="utf-8")

    assert "Check ETF stock-flow read-model readiness" in workflow
    assert "etfStockFlow.available == true" in workflow
    assert "((.etfStockFlow.coverage.stockCount // 0) > 0)" in workflow
    assert 'steps.stock-flow.outputs.ready' in workflow
    readiness_index = workflow.index("Check ETF stock-flow read-model readiness")
    build_index = workflow.index("Build immutable read models offline")
    publish_index = workflow.index("Publish immutable read models")
    assert readiness_index < build_index < publish_index


def test_stock_flow_history_is_bounded_and_current_partition_is_required() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/daily-data.yml").read_text(encoding="utf-8")

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
