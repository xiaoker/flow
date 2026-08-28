"""Safe, exact-date parsing for the SSE ETF PCF XML endpoint.

The parser has no network or filesystem side effects.  It is used by the PR 1
shadow adapter and will become the shared parser for the resumable collector in
PR 2.  Unknown component fields are retained in ``source_extra_json`` so the
canonical layer can remain low-loss when the exchange adds a tag.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from defusedxml import ElementTree

from .etf_stock_flow import normalise_underlying_code


MAX_XML_BYTES = 5 * 1024 * 1024
_DATE = re.compile(r"^\d{8}$")
_KNOWN_HEADER = {
    "fundinstrumentid",
    "fundid2",
    "fundname",
    "fundfullname",
    "tradingday",
    "pretradingday",
    "creationredemptionunit",
    "creationunit",
    "recordnumber",
}
_KNOWN_COMPONENT = {
    "instrumentid",
    "instrumentname",
    "quantity",
    "substitutionflag",
    "creationpremiumrate",
    "redemptiondiscountrate",
    "substitutioncashamount",
    "underlyingsecurityid",
    "underlyionsecurityid",
}


def hash_normalized_pcf_rows(rows: list[dict[str, Any]]) -> str:
    """Return the stable per-ETF digest used across capture and finalize.

    The digest deliberately covers the parser output before release-specific
    lineage fields are added.  This lets the streaming Worker persist one
    small integrity fact without decoding the XML or canonical CSV.
    """

    payload = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower().replace("_", "")


def _text(element: Any) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _descendants(root: Any, names: set[str]) -> list[Any]:
    return [item for item in root.iter() if _local_name(str(item.tag)) in names]


def _first_text(root: Any, names: set[str]) -> str | None:
    for item in _descendants(root, names):
        value = _text(item)
        if value:
            return value
    return None


def _date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not _DATE.fullmatch(text):
        raise ValueError(f"{field}_invalid")
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _integer(value: str | None, field: str, *, allow_zero: bool = True) -> int:
    if value is None or not value.strip():
        raise ValueError(f"{field}_missing")
    try:
        number = float(value.replace(",", "").strip())
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(number) or not number.is_integer() or (not allow_zero and number <= 0) or number < 0:
        raise ValueError(f"{field}_invalid")
    return int(number)


def _component_elements(root: Any) -> list[Any]:
    return [item for item in root.iter() if _local_name(str(item.tag)) == "component"]


def _header_extra(root: Any) -> dict[str, str]:
    """Keep unknown top-level scalar fields without copying component data."""

    extra: dict[str, str] = {}
    for element in list(root):
        name = _local_name(str(element.tag))
        if name in _KNOWN_HEADER or name in {"componentlist", "component"}:
            continue
        if list(element):
            continue
        value = _text(element)
        if value:
            extra[name] = value
    return extra


def parse_sse_etf_pcf_xml(
    content: bytes,
    *,
    requested_code: str,
    target_date: str,
    previous_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Return normalized component rows only for the exact D/D-1 contract."""

    if len(content) > MAX_XML_BYTES:
        raise ValueError("pcf_xml_too_large")
    try:
        root = ElementTree.fromstring(content)
    except Exception as exc:
        raise ValueError("pcf_xml_malformed_or_unsafe") from exc

    fund_code = _first_text(root, {"fundinstrumentid", "fundid2"})
    trading_day = _date(_first_text(root, {"tradingday"}), "trading_day")
    pre_trading_day = _date(_first_text(root, {"pretradingday"}), "pre_trading_day")
    header: dict[str, Any] = {
        "fundInstrumentId": fund_code,
        "tradingDay": trading_day,
        "preTradingDay": pre_trading_day,
        "fundName": _first_text(root, {"fundname", "fundfullname"}),
        "sourceExtraJson": json.dumps(_header_extra(root), ensure_ascii=False, sort_keys=True),
    }
    warnings: list[str] = []
    if fund_code is None or str(fund_code).strip().zfill(6) != str(requested_code).strip().zfill(6):
        warnings.append("基金代码与请求代码不一致")
    if trading_day != target_date:
        warnings.append(f"TradingDay={trading_day or '缺失'} 不等于目标日 {target_date}")
    if pre_trading_day != previous_date:
        warnings.append(f"PreTradingDay={pre_trading_day or '缺失'} 不等于前一交易日 {previous_date}")
    if warnings:
        return [], header, warnings

    creation_unit = _integer(
        _first_text(root, {"creationredemptionunit", "creationunit"}),
        "creation_unit",
        allow_zero=False,
    )
    components = _component_elements(root)
    record_number_text = _first_text(root, {"recordnumber"})
    if record_number_text is not None:
        record_number = _integer(record_number_text, "record_number")
        if record_number != len(components):
            raise ValueError("record_number_component_count_mismatch")
    header["creationRedemptionUnit"] = creation_unit
    header["recordNumber"] = len(components)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for component in components:
        values = {
            _local_name(str(child.tag)): _text(child)
            for child in list(component)
        }
        instrument_id = values.get("instrumentid", "").strip()
        listing_market = values.get("underlyingsecurityid", values.get("underlyionsecurityid", "")).strip()
        key = (listing_market, instrument_id)
        if key in seen:
            raise ValueError("duplicate_component_key")
        seen.add(key)
        if not instrument_id:
            raise ValueError("component_instrument_id_missing")
        quantity = _integer(values.get("quantity"), "quantity")
        unknown = {key: value for key, value in values.items() if key not in _KNOWN_COMPONENT}
        rows.append(
            {
                "market": "SSE",
                "etf_code": str(requested_code).strip().zfill(6),
                "etf_name": header["fundName"] or str(requested_code).strip().zfill(6),
                "stock_code": normalise_underlying_code(instrument_id, listing_market),
                "stock_name": values.get("instrumentname") or instrument_id,
                "quantity": quantity,
                "creation_unit": creation_unit,
                "substitution_flag": values.get("substitutionflag", ""),
                "listing_market": listing_market,
                "source_extra_json": json.dumps(unknown, ensure_ascii=False, sort_keys=True),
            }
        )
    return rows, header, []
