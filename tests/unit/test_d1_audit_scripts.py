from __future__ import annotations

from scripts.audit_etf_daily_duplicates import _unwrap_wrangler_json, summarize
from scripts.report_d1_usage import aggregate


def test_etf_daily_audit_reports_business_key_multiplier_and_redundancy() -> None:
    rows = [
        {"release_id": "r1", "trade_date": "2026-08-28", "market": "SSE", "instrument_code": "510300"},
        {"release_id": "r2", "trade_date": "2026-08-28", "market": "SSE", "instrument_code": "510300", "release_status": "SUPERSEDED"},
        {"release_id": "r1", "trade_date": "2026-08-28", "market": "SZSE", "instrument_code": "159919", "data_release_exists": False},
    ]
    report = summarize(rows)
    assert report["total_rows"] == 3
    assert report["unique_business_keys"] == 2
    assert report["materialization_multiplier"] == 1.5
    assert report["redundant_rows"] == 1
    assert report["orphan_rows"] == 1
    assert report["superseded_rows"] == 1


def test_usage_report_aggregates_preaggregated_fingerprints() -> None:
    report = aggregate(
        [
            {"database": "flow", "queryFingerprint": "history", "rowsRead": 10, "rowsWritten": 0, "count": 3},
            {"database": "flow", "queryFingerprint": "insert", "rowsRead": 0, "rowsWritten": 4},
            {"database": "risk-sizer", "queryFingerprint": "read", "rowsRead": 8, "rowsWritten": 0},
        ]
    )
    assert report["totals"] == {"rowsRead": 38, "rowsWritten": 4}
    assert report["byDatabase"]["flow"] == {"rowsRead": 30, "rowsWritten": 4, "records": 4}
    assert report["topFingerprints"][0]["fingerprint"] == "history"


def test_usage_report_preserves_explicit_zero_call_count() -> None:
    report = aggregate(
        [{"database": "flow", "queryFingerprint": "skipped", "rowsRead": 100, "count": 0}]
    )
    assert report["totals"] == {"rowsRead": 0, "rowsWritten": 0}


def test_wrangler_envelope_is_unwrapped() -> None:
    payload = [{"results": [{"total_rows": 12, "unique_business_keys": 4}]}]
    assert _unwrap_wrangler_json(payload) == {
        "total_rows": 12,
        "unique_business_keys": 4,
        "release_count": 0,
        "orphan_rows": 0,
        "superseded_rows": 0,
    }
