from __future__ import annotations

import json
from pathlib import Path

from trading_desk.etf_query_plan import (
    EtfQueryPlanFixture,
    PRODUCTION_SCALE,
    build_query_plan_report,
)


def test_production_scale_fixture_descriptor_is_stable() -> None:
    descriptor = json.loads(
        (Path(__file__).parents[1] / "fixtures/etf_query_plan/prod-scale.json").read_text()
    )
    assert PRODUCTION_SCALE.to_dict() == {
        "trading_days": descriptor["tradingDays"],
        "etf_count": descriptor["etfCount"],
        "revisions_per_business_key": descriptor["revisionsPerBusinessKey"],
        "expected_rows": descriptor["expectedRows"],
    }


def test_legacy_query_plan_report_is_reproducible_without_d1() -> None:
    report = build_query_plan_report(
        EtfQueryPlanFixture(trading_days=8, etf_count=16, revisions_per_business_key=3)
    )
    assert report["queryId"] == "loadOfficialEtfDailyChanges"
    assert report["materialized"] == {
        "etfDailyRows": 8 * 16 * 3,
        "previousWindowRows": 7 * 16 * 3,
        "tradingDateCount": 8,
    }
    assert report["signals"]["usesRowNumber"] is True
    assert report["signals"]["hasHistoricalPredicate"] is True
    assert report["signals"]["referencesEtfDaily"] is True
    assert report["signals"]["estimatedProductionPreviousWindowRows"] == 364 * 1600 * 5
    assert report["plan"]
