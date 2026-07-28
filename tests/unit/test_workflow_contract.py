from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_daily_collection_runs_on_business_mornings_not_saturday() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")

    assert '- cron: "15 1 * * 1-5"' in workflow
    assert '- cron: "15 1 * * 2-6"' not in workflow
    assert "weekends must not be scheduled" in workflow


def test_daily_collection_retries_network_failures_before_building_snapshot() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/daily-data.yml"
    ).read_text(encoding="utf-8")

    assert "MAX_ATTEMPTS=3" in workflow
    assert '.error_type == "ConnectionError"' in workflow
    assert '.error_type == "HTTPError"' in workflow
    assert workflow.index("Enforce publication quality gate") < workflow.index(
        "Build dashboard snapshot"
    )


def test_sse_relay_has_an_independent_scheduled_smoke_check() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github/workflows/access-smoke.yml"
    ).read_text(encoding="utf-8")

    assert '- cron: "30 0 * * 1"' in workflow
    assert "SOURCE_PROXY_URL: https://flow.xiaoker.com/api/v1/admin/source-proxy" in workflow
    assert "Authorization: Bearer $INGEST_API_KEY" in workflow
    assert "sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L" in workflow
    assert "Authenticated, allowlisted SSE fallback transport is healthy." in workflow
