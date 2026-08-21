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
    assert '"15 1 * * mon-fri"' in wrangler
    assert '"45 1 * * mon-fri"' in wrangler
    assert "GITHUB_ACTIONS_TOKEN" in worker
    assert "daily-data.yml/dispatches" in worker
    assert 'ref: "main"' in worker
    assert 'inputs: { collection_window: window }' in worker
    assert 'cron.startsWith("15 1")' in worker
    assert 'cron.startsWith("45 1")' in worker
    assert 'return "reconcile"' in worker
    assert "response.status === 200 || response.status === 204" in worker
    assert 'console.error("Daily collection dispatch failed"' in worker
    assert "DISPATCH_RETRY_DELAYS_MS = [0, 1_000, 3_000]" in worker
    assert "LATE_SCHEDULE_THRESHOLD_MS = 5 * 60 * 1_000" in worker
    assert "isRetryableDispatchStatus" in worker
    assert "scheduleLagMs" in worker
    assert 'console.warn("GitHub workflow dispatch will be retried"' in worker
    assert "shouldDispatchReconcile" in worker
    assert "source_readiness" in worker
    assert 'X-Flow-Build-Sha' in worker


def test_worker_deploy_publishes_build_identity() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-worker.yml").read_text(encoding="utf-8")
    assert "npm run build" in workflow
    assert "npx wrangler deploy" in workflow
    assert '--var "FLOW_BUILD_SHA:${GITHUB_SHA}"' in workflow
    assert "FLOW_DEPLOYED_AT" in workflow


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
    assert "actions/checkout@8e8c483db84b4bee98b60c0593521ed34d9990e8" in workflow_text
    assert "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c" in workflow_text
    assert "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f" in workflow_text


def test_daily_collection_retries_network_failures_and_archives_pending_sources() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")

    assert "MAX_ATTEMPTS=3" in workflow
    assert "SOURCE_READINESS_ATTEMPTS=18" not in workflow
    assert "sleep 120" not in workflow
    assert "id: collect" in workflow
    assert 'echo "pending=true" >> "$GITHUB_OUTPUT"' in workflow
    assert 'source_pending="${{ steps.collect.outputs.pending }}"' in workflow
    assert "Official source date is not ready" in workflow
    assert "Official sources passed the collection gate" in workflow
    assert 'phase0_gate_status == "pending"' in workflow
    assert "Record source readiness ledger" in workflow
    assert "build_source_readiness.py" in workflow
    assert "api/v1/admin/source-readiness" in workflow
    assert "source-readiness.json" in workflow
    assert '- cron: "15 1 * * 1-5"' in workflow
    assert '- cron: "45 1 * * 1-5"' in workflow
    assert "Collection window pending" in workflow
    assert 'timeliness.requested_date_coverage' in workflow
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
    assert workflow.index("Stage immutable read-model candidate") < workflow.index(
        "Verify staged read-model candidate"
    ) < workflow.index("Activate canary-verified read-model candidate")
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
    assert "--result-output read-model-publish.json" in workflow
    assert "--stage-only" in workflow
    assert "--candidate" in workflow
    assert "activate_read_models.py" in workflow
    assert '.status == "staged_candidate"' in workflow
    assert '.mode == "candidate"' in workflow
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
    publish_index = workflow.index("Stage immutable read-model candidate")
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
