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
