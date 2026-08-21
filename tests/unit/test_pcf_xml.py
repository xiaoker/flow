from __future__ import annotations

from pathlib import Path

import pytest

from trading_desk.pcf_xml import parse_sse_etf_pcf_xml


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "pcf"


def test_parser_rejects_rolled_over_date_before_normalization() -> None:
    content = (FIXTURE_ROOT / "next-day-rolled-over.xml").read_bytes()

    rows, header, warnings = parse_sse_etf_pcf_xml(
        content,
        requested_code="510300",
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )

    assert rows == []
    assert header["tradingDay"] == "2026-08-24"
    assert any("TradingDay" in warning for warning in warnings)


def test_parser_preserves_zero_quantity_and_unknown_component_fields() -> None:
    zero_rows, _, _ = parse_sse_etf_pcf_xml(
        (FIXTURE_ROOT / "zero-quantity-cash-substitution.xml").read_bytes(),
        requested_code="510300",
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )
    unknown_rows, _, _ = parse_sse_etf_pcf_xml(
        (FIXTURE_ROOT / "unknown-component-tag.xml").read_bytes(),
        requested_code="510500",
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )

    assert zero_rows[0]["quantity"] == 0
    assert zero_rows[0]["substitution_flag"] == "1"
    assert "futureofficialfield" in unknown_rows[0]["source_extra_json"]


def test_parser_rejects_dtd_and_external_entity_fixture() -> None:
    with pytest.raises(ValueError, match="pcf_xml_malformed_or_unsafe"):
        parse_sse_etf_pcf_xml(
            (FIXTURE_ROOT / "malicious-dtd.xml").read_bytes(),
            requested_code="510300",
            target_date="2026-08-21",
            previous_date="2026-08-20",
        )
