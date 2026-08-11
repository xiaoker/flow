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
    assert '- cron: "25 0 * * 1-5"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert '"crons": ["30 10 * * mon-fri", "45 23 * * sun-thu", "10 0 * * mon-fri"]' in wrangler
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
    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803" in workflow_text
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in workflow_text
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow_text


def test_daily_collection_retries_network_failures_and_archives_pending_sources() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")

    assert "MAX_ATTEMPTS=3" in workflow
    assert "SOURCE_READINESS_ATTEMPTS=18" in workflow
    assert "SZSE source date is not ready" in workflow
    assert 'timeliness.requested_date_coverage' in workflow
    assert '.error_type == "ConnectionError"' in workflow
    assert '.error_type == "HTTPError"' in workflow
    assert workflow.index("Build dashboard snapshot") < workflow.index(
        "Build verified archive and publish bundle"
    )
    assert "Build immutable read models offline" in workflow
    assert "--require-materialized" in workflow
    assert "--read-model-dir read-models" in workflow
    assert '"Snapshot is pending source confirmation' in workflow
    assert "Fail loudly on unresolved source errors" in workflow
    assert "Collection archived partial evidence but still has unresolved source errors." in workflow


def test_sse_relay_has_an_independent_scheduled_smoke_check() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/access-smoke.yml"
    ).read_text(encoding="utf-8")

    assert '- cron: "30 0 * * 1"' in workflow
    assert "SOURCE_PROXY_URL: https://flow.xiaoker.com/api/v1/admin/source-proxy" in workflow
    assert "Authorization: Bearer $INGEST_API_KEY" in workflow
    assert "sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L" in workflow
    assert "Authenticated, allowlisted SSE fallback transport is healthy." in workflow
