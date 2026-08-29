"""Build and reconcile the compact official ETF adjacent-change contract.

The historical ETF share table contains source revisions and is therefore a
poor place to discover the previous business date at request time.  The
collector already has the canonical SSE/SZSE daily frame, so this module
derives the adjacent change once, offline, and carries the evidence in the
history bundle.  It deliberately never performs network or database I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from typing import Any

import pandas as pd


OFFICIAL_ETF_DAILY_CHANGES_SCHEMA_VERSION = 1
OFFICIAL_ETF_MARKETS = ("SSE", "SZSE")
_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


def _iso_date(value: Any, *, field: str, allow_none: bool = False) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        if allow_none:
            return None
        raise ValueError(f"{field}_missing")
    text = str(value).strip()
    if not re.fullmatch(_ISO_DATE, text):
        raise ValueError(f"{field}_invalid:{text}")
    return text


def _integer(value: Any, *, field: str, allow_none: bool = False) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        if allow_none:
            return None
        raise ValueError(f"{field}_missing")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(number) or number != round(number):
        raise ValueError(f"{field}_invalid")
    result = int(round(number))
    if result < 0 and field in {"currentShares", "previousShares"}:
        raise ValueError(f"{field}_negative")
    return result


def _normalise_frame(canonical_frame: pd.DataFrame, *, as_of: str) -> pd.DataFrame:
    required = {"trade_date", "market", "instrument_code", "shares"}
    missing = required - set(canonical_frame.columns)
    if missing:
        raise ValueError(f"canonical_etf_columns_missing:{','.join(sorted(missing))}")
    value = canonical_frame.copy()
    value["trade_date"] = value["trade_date"].map(
        lambda item: _iso_date(item, field="trade_date")
    )
    value["market"] = value["market"].astype(str).str.strip().str.upper()
    value["instrument_code"] = (
        value["instrument_code"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )
    value["shares"] = pd.to_numeric(value["shares"], errors="coerce")
    if value["shares"].isna().any() or not value["shares"].map(
        lambda number: math.isfinite(float(number)) and float(number).is_integer()
    ).all():
        raise ValueError("canonical_etf_shares_invalid")
    if value["shares"].lt(0).any():
        raise ValueError("canonical_etf_shares_negative")
    if (~value["market"].isin(OFFICIAL_ETF_MARKETS)).any():
        raise ValueError("canonical_etf_market_invalid")
    if (~value["instrument_code"].str.fullmatch(r"\d{6}")).any():
        raise ValueError("canonical_etf_code_invalid")
    if (value["trade_date"] > as_of).any():
        raise ValueError("canonical_etf_future_date")
    key = ["trade_date", "market", "instrument_code"]
    if value.duplicated(key).any():
        raise ValueError("canonical_etf_duplicate_business_key")
    value["shares"] = value["shares"].round().astype("int64")
    return value.sort_values(key, kind="mergesort").reset_index(drop=True)


def _text_or_empty(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _row_fingerprint_fields(row: dict[str, Any]) -> list[str]:
    reason = str(row.get("alignmentReason") or ("aligned" if row.get("aligned") else "missing_previous"))
    return [
        str(row["market"]),
        str(row["code"]),
        _text_or_empty(row.get("previousTradeDate")),
        _text_or_empty(row.get("currentShares")),
        _text_or_empty(row.get("previousShares")),
        _text_or_empty(row.get("changeShares")),
        "1" if row.get("aligned") else "0",
        reason,
    ]


def official_etf_daily_changes_fingerprint(
    *,
    as_of: str,
    expected_previous_date: str | None,
    source_release_ids: Iterable[str],
    rows: Iterable[dict[str, Any]],
) -> str:
    """Return the stable input fingerprint carried by the wire contract.

    The hash covers the exact adjacent rows and source release ids.  It is
    intentionally independent of JSON object key order and therefore can be
    reproduced by the Worker without reading historical D1 rows.
    """

    release_ids = sorted({str(value) for value in source_release_ids if str(value)})
    ordered_rows = sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row.get("market")), str(row.get("code"))),
    )
    lines = [str(as_of), expected_previous_date or "", ",".join(release_ids)]
    lines.extend(",".join(_row_fingerprint_fields(row)) for row in ordered_rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def build_official_etf_daily_changes(
    canonical_frame: pd.DataFrame,
    *,
    as_of: str,
    expected_previous_date: str | None = None,
    source_release_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Build adjacent ETF share changes from one canonical daily frame.

    Rows are emitted for the union of the current and expected-previous
    universe.  Missing previous values, new listings, delayed markets and
    delistings are represented explicitly; an unaligned row never receives a
    fabricated 1d change.
    """

    as_of = _iso_date(as_of, field="as_of") or ""
    value = _normalise_frame(canonical_frame, as_of=as_of)
    available_dates = sorted(value["trade_date"].unique().tolist())
    if expected_previous_date is None:
        earlier = [date for date in available_dates if date < as_of]
        expected_previous_date = earlier[-1] if earlier else None
    else:
        expected_previous_date = _iso_date(
            expected_previous_date, field="expected_previous_date", allow_none=True
        )
        if expected_previous_date is not None and expected_previous_date >= as_of:
            raise ValueError("expected_previous_date_not_before_as_of")

    source_ids = sorted({str(value) for value in source_release_ids if str(value)})
    current = value.loc[value["trade_date"].eq(as_of)].copy()
    previous = (
        value.loc[value["trade_date"].eq(expected_previous_date)].copy()
        if expected_previous_date is not None
        else value.iloc[0:0].copy()
    )
    current_by_key = {
        (str(row.market), str(row.instrument_code)): int(row.shares)
        for row in current.itertuples(index=False)
    }
    previous_by_key = {
        (str(row.market), str(row.instrument_code)): int(row.shares)
        for row in previous.itertuples(index=False)
    }
    all_keys = sorted(set(current_by_key) | set(previous_by_key))

    market_latest: dict[str, str | None] = {}
    market_has_current: dict[str, bool] = {}
    for market in OFFICIAL_ETF_MARKETS:
        market_rows = value.loc[value["market"].eq(market)]
        market_latest[market] = (
            str(market_rows["trade_date"].max()) if not market_rows.empty else None
        )
        market_has_current[market] = bool(
            market_rows["trade_date"].eq(as_of).any()
        )
    delayed_markets = {
        market
        for market in OFFICIAL_ETF_MARKETS
        if not market_has_current[market] and market_latest[market] != as_of
    }

    rows: list[dict[str, Any]] = []
    reason_counts = {
        "aligned": 0,
        "new_listing": 0,
        "missing_previous": 0,
        "delisted": 0,
        "delayed_market": 0,
    }
    for market, code in all_keys:
        current_shares = current_by_key.get((market, code))
        previous_shares = previous_by_key.get((market, code))
        prior_rows = value.loc[
            (value["market"].eq(market))
            & (value["instrument_code"].eq(code))
            & (value["trade_date"] < as_of)
        ]
        actual_previous_date = (
            str(prior_rows["trade_date"].max()) if not prior_rows.empty else None
        )
        if current_shares is None:
            # If the whole market has not reached the as-of date, absence of a
            # current row is a publication delay, not evidence of delisting.
            reason = "delayed_market" if market in delayed_markets else "delisted"
            aligned = False
            change_shares = None
        elif (
            expected_previous_date is not None
            and previous_shares is not None
            and actual_previous_date == expected_previous_date
        ):
            reason = "aligned"
            aligned = True
            change_shares = current_shares - previous_shares
        elif actual_previous_date is None:
            reason = "new_listing"
            aligned = False
            change_shares = None
        else:
            reason = "missing_previous"
            aligned = False
            change_shares = None
        reason_counts[reason] += 1
        rows.append(
            {
                "market": market,
                "code": code,
                "previousTradeDate": actual_previous_date,
                "currentShares": current_shares,
                "previousShares": previous_shares,
                "changeShares": change_shares,
                "aligned": aligned,
                "alignmentReason": reason,
            }
        )

    market_status: list[dict[str, Any]] = []
    for market in OFFICIAL_ETF_MARKETS:
        market_rows = value.loc[value["market"].eq(market)]
        market_current = market_rows.loc[market_rows["trade_date"].eq(as_of)]
        latest_available = (
            str(market_rows["trade_date"].max()) if not market_rows.empty else None
        )
        if market_current.empty:
            status = "delayed_market" if latest_available != as_of else "missing_current"
        elif expected_previous_date is None or not market_rows["trade_date"].eq(expected_previous_date).any():
            status = "missing_previous"
        else:
            status = "ready"
        market_status.append(
            {
                "market": market,
                "status": status,
                "latestAvailableDate": latest_available,
                "currentDate": as_of if not market_current.empty else None,
                "expectedPreviousDate": expected_previous_date,
            }
        )

    counts = {
        "totalRows": len(rows),
        "alignedRows": reason_counts["aligned"],
        "newListingRows": reason_counts["new_listing"],
        "missingPreviousRows": reason_counts["missing_previous"],
        "delistedRows": reason_counts["delisted"],
        "delayedMarketRows": reason_counts["delayed_market"],
    }
    contract = {
        "schemaVersion": OFFICIAL_ETF_DAILY_CHANGES_SCHEMA_VERSION,
        "asOf": as_of,
        "expectedPreviousDate": expected_previous_date,
        "sourceReleaseIds": source_ids,
        "inputSha256": official_etf_daily_changes_fingerprint(
            as_of=as_of,
            expected_previous_date=expected_previous_date,
            source_release_ids=source_ids,
            rows=rows,
        ),
        "rows": rows,
        "marketStatus": market_status,
        "counts": counts,
    }
    return contract


def _legacy_row_value(row: dict[str, Any]) -> int | None:
    if row.get("changeShares") is not None:
        return int(row["changeShares"])
    value = row.get("value")
    if value is None:
        return None
    return int(round(float(value) * 100_000_000))


def reconcile_official_etf_daily_changes(
    offline_contract: dict[str, Any],
    legacy_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare offline rows with one captured legacy query result.

    The result is a read-only report.  It is intentionally explicit about the
    market/code/date dimensions so a mismatch cannot hide in an aggregate.
    """

    offline_rows = [dict(row) for row in offline_contract.get("rows") or []]
    legacy_rows = [dict(row) for row in legacy_rows]

    def key(row: dict[str, Any]) -> tuple[str, str]:
        return (str(row.get("market") or ""), str(row.get("code") or row.get("instrument_code") or "").zfill(6))

    offline_by_key = {key(row): row for row in offline_rows}
    legacy_by_key = {key(row): row for row in legacy_rows}
    differences: list[dict[str, Any]] = []
    for business_key in sorted(set(offline_by_key) | set(legacy_by_key)):
        offline = offline_by_key.get(business_key)
        legacy = legacy_by_key.get(business_key)
        market, code = business_key
        fields: dict[str, Any] = {}
        if offline is None:
            fields["row"] = "offline_missing"
        elif legacy is None:
            fields["row"] = "legacy_missing"
        else:
            offline_previous = offline.get("previousTradeDate")
            legacy_previous = legacy.get("previousTradeDate", legacy.get("previous_trade_date"))
            if offline_previous != legacy_previous:
                fields["previousTradeDate"] = {
                    "offline": offline_previous,
                    "legacy": legacy_previous,
                }
            legacy_expected_present = (
                "expectedPreviousTradeDate" in legacy
                or "expected_previous_trade_date" in legacy
            )
            legacy_expected = legacy.get(
                "expectedPreviousTradeDate", legacy.get("expected_previous_trade_date")
            )
            if legacy_expected_present and legacy_expected != offline_contract.get("expectedPreviousDate"):
                fields["expectedPreviousDate"] = {
                    "offline": offline_contract.get("expectedPreviousDate"),
                    "legacy": legacy_expected,
                }
            offline_current = offline.get("currentShares")
            legacy_current = legacy.get("currentShares", legacy.get("current_shares"))
            if offline_current != legacy_current:
                fields["currentShares"] = {"offline": offline_current, "legacy": legacy_current}
            offline_previous_shares = offline.get("previousShares")
            legacy_previous_shares = legacy.get("previousShares", legacy.get("previous_shares"))
            if offline_previous_shares != legacy_previous_shares:
                fields["previousShares"] = {
                    "offline": offline_previous_shares,
                    "legacy": legacy_previous_shares,
                }
            legacy_reason = legacy.get("alignmentReason", legacy.get("reason"))
            if legacy_reason is not None and legacy_reason != offline.get("alignmentReason"):
                fields["alignmentReason"] = {
                    "offline": offline.get("alignmentReason"),
                    "legacy": legacy_reason,
                }
            offline_change = offline.get("changeShares")
            legacy_change = _legacy_row_value(legacy)
            if offline_change != legacy_change:
                fields["changeShares"] = {"offline": offline_change, "legacy": legacy_change}
        if fields:
            differences.append({"market": market, "code": code, "fields": fields})

    offline_total = sum(
        int(row["changeShares"])
        for row in offline_rows
        if row.get("changeShares") is not None
    )
    legacy_total = sum(
        value
        for value in (_legacy_row_value(row) for row in legacy_rows)
        if value is not None
    )
    aggregate = {
        "offlineChangeShares": offline_total,
        "legacyChangeShares": legacy_total,
        "deltaShares": offline_total - legacy_total,
        "offlineAlignedRows": sum(bool(row.get("aligned")) for row in offline_rows),
        "legacyAlignedRows": sum(_legacy_row_value(row) is not None for row in legacy_rows),
    }
    comparison_rows = len(set(offline_by_key) | set(legacy_by_key))
    matched_rows = comparison_rows - len(differences)
    by_market: dict[str, dict[str, int]] = {}
    for market in sorted({*{key(row)[0] for row in offline_rows}, *{key(row)[0] for row in legacy_rows}}):
        offline_market = [row for row in offline_rows if key(row)[0] == market]
        legacy_market = [row for row in legacy_rows if key(row)[0] == market]
        offline_value = sum(
            int(row["changeShares"])
            for row in offline_market
            if row.get("changeShares") is not None
        )
        legacy_value = sum(
            value
            for value in (_legacy_row_value(row) for row in legacy_market)
            if value is not None
        )
        by_market[market] = {
            "offlineChangeShares": offline_value,
            "legacyChangeShares": legacy_value,
            "deltaShares": offline_value - legacy_value,
            "offlineRows": len(offline_market),
            "legacyRows": len(legacy_market),
        }
    return {
        "asOf": offline_contract.get("asOf"),
        "expectedPreviousDate": offline_contract.get("expectedPreviousDate"),
        "status": "pass" if not differences and aggregate["deltaShares"] == 0 else "mismatch",
        "offlineRows": len(offline_rows),
        "legacyRows": len(legacy_rows),
        "differenceCount": len(differences),
        "matchedRows": matched_rows,
        "matchRate": 1.0 if comparison_rows == 0 else matched_rows / comparison_rows,
        "differences": differences,
        "byMarket": by_market,
        "aggregate": aggregate,
    }


def contract_json(contract: dict[str, Any]) -> str:
    """Encode a contract deterministically for reports and tests."""

    return json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
