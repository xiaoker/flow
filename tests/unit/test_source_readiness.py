from __future__ import annotations

from trading_desk.source_readiness import build_source_readiness


def _inputs(*, target: str = "2026-08-20", missing: set[str] | None = None, errors=None):
    missing = missing or set()
    datasets = {
        "sse_etf": [target],
        "szse_etf": [target],
        "sse_margin": [target],
        "szse_margin": [target],
        "bse_margin": [target],
        "stock_connect_summary": [target],
        "sse_etf_nav": [],
        "sse_etf_pcf": [],
    }
    profiles = {
        key: {"distinct_trade_dates": ([] if key in missing else dates)}
        for key, dates in datasets.items()
    }
    findings = []
    for key in missing:
        findings.append(
            {
                "check_id": "timeliness.requested_date_coverage",
                "dataset": key,
                "status": "fail",
                "evidence": {"missing_dates": [target]},
                "message": "未覆盖全部请求日期",
            }
        )
    manifest = {
        "run_id": "run-20260820-1",
        "finished_at": "2026-08-20T01:00:00Z",
        "requested_dates": ["2026-08-18", "2026-08-19", target],
        "szse_etf_page_date": "2026-08-21" if "szse_etf" not in missing else "2026-08-20",
        "szse_etf_trade_date": target if "szse_etf" not in missing else "2026-08-19",
        "combined_paths": {key: f"data/{key}.csv" for key in datasets},
        "quality_summary_path": "reports/phase0/latest-quality-summary.json",
    }
    quality = {"profiles": profiles, "findings": findings, "errors": errors or []}
    return manifest, quality


def test_source_date_gap_is_pending_and_has_stable_deadline() -> None:
    manifest, quality = _inputs(missing={"szse_etf", "szse_margin"})
    ledger = build_source_readiness(
        manifest,
        quality,
        now=__import__("datetime").datetime.fromisoformat("2026-08-20T01:15:00+00:00"),
    )

    assert ledger["state"] == "pending"
    assert ledger["pendingDomains"] == ["深交所 ETF", "深交所融资融券"]
    assert ledger["deadlineAt"] == "2026-08-20T03:30:00.000Z"
    rows = {row["sourceId"]: row for row in ledger["sources"]}
    assert rows["szse_etf"]["status"] == "pending"
    assert rows["sse_etf"]["status"] == "ready"
    assert rows["sse_etf_nav"]["required"] is False


def test_source_parser_error_is_error_not_pending() -> None:
    manifest, quality = _inputs(errors=[{"step": "szse_margin:2026-08-20", "error_type": "ValueError", "message": "schema changed"}])
    ledger = build_source_readiness(manifest, quality)

    assert ledger["state"] == "error"
    row = next(item for item in ledger["sources"] if item["sourceId"] == "szse_margin")
    assert row["status"] == "error"
    assert row["reason"] == "schema changed"


def test_repeated_run_increments_attempt_but_same_run_is_idempotent() -> None:
    manifest, quality = _inputs(missing={"szse_etf"})
    previous = {
        "sources": [
            {"sourceId": "szse_etf", "attempt": 2, "runId": "older-run"},
            {"sourceId": "sse_etf", "attempt": 4, "runId": manifest["run_id"]},
        ]
    }
    ledger = build_source_readiness(manifest, quality, previous=previous)
    rows = {row["sourceId"]: row for row in ledger["sources"]}
    assert rows["szse_etf"]["attempt"] == 3
    assert rows["sse_etf"]["attempt"] == 4


def test_skipped_duplicate_pcf_window_preserves_prior_ready_state() -> None:
    manifest, quality = _inputs()
    manifest["pcf_capture_status"] = "skipped"
    previous = {
        "sources": [
            {
                "sourceId": "sse_etf_pcf",
                "status": "ready",
                "attempt": 1,
                "runId": "previous-run",
                "observedDates": ["2026-08-20"],
                "watermark": {"observedTradeDate": "2026-08-20"},
            }
        ]
    }

    ledger = build_source_readiness(manifest, quality, previous=previous)
    row = next(item for item in ledger["sources"] if item["sourceId"] == "sse_etf_pcf")

    assert row["status"] == "ready"
    assert row["observedDates"] == ["2026-08-20"]
    assert row["reason"] == "official_source_target_date_observed_previous_window"


def test_all_required_sources_ready_even_when_optional_enrichment_is_missing() -> None:
    manifest, quality = _inputs()
    ledger = build_source_readiness(manifest, quality)

    assert ledger["state"] == "ready"
    assert ledger["pendingDomains"] == []
    assert next(row for row in ledger["sources"] if row["sourceId"] == "sse_etf_nav")["status"] == "pending"
