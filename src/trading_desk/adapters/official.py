from __future__ import annotations

import io
import json
import re
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..http import get
from ..instrument_names import clean_instrument_name, resolve_hkex_instrument_name
from ..models import FetchArtifact, SourceSpec
from ..etf_stock_flow import normalise_underlying_code
from ..storage import write_normalized_csv, write_raw_response


SSE_ETF = SourceSpec(
    source_id="sse",
    publisher="上海证券交易所",
    dataset="etf_scale",
    url="https://query.sse.com.cn/commonQuery.do",
)
SSE_ETF_NAV = SourceSpec(
    source_id="sse",
    publisher="上海证券交易所",
    dataset="etf_nav",
    url="https://query.sse.com.cn/commonQuery.do",
    date_semantics="source_publication_previous_trading_day_nav",
)
SSE_ETF_PCF = SourceSpec(
    source_id="sse",
    publisher="上海证券交易所",
    dataset="etf_pcf",
    url="https://query.sse.com.cn/commonQuery.do",
    date_semantics="sse_pcf_current_day_or_previous_day_basket",
)
SZSE_ETF = SourceSpec(
    source_id="szse",
    publisher="深圳证券交易所",
    dataset="etf_scale",
    url="https://fund.szse.cn/api/report/ShowReport",
    date_semantics="source_page_previous_trading_day",
)
SSE_MARGIN = SourceSpec(
    source_id="sse",
    publisher="上海证券交易所",
    dataset="margin_market_daily",
    url="https://query.sse.com.cn/marketdata/tradedata/queryMargin.do",
)
SZSE_MARGIN = SourceSpec(
    source_id="szse",
    publisher="深圳证券交易所",
    dataset="margin_market_daily",
    url="https://www.szse.cn/api/report/ShowReport/data",
)
BSE_MARGIN = SourceSpec(
    source_id="bse",
    publisher="北京证券交易所",
    dataset="margin_market_daily",
    url="https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do",
    date_semantics="official_previous_trading_day_summary",
)
HKEX_CONNECT = SourceSpec(
    source_id="hkex",
    publisher="香港交易所",
    dataset="stock_connect_daily",
    url="https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_[YYYY][MM][DD]c.js",
)

SZSE_ETF_PAGE_URL = "https://fund.szse.cn/marketdata/fundslist/index.html"
SZSE_ETF_METADATA_URL = "https://fund.szse.cn/api/report/ShowReport/data"
BSE_MARGIN_PAGE_URL = "https://www.bse.cn/disclosure/rzrq_trans_list.html"
BSE_MARGIN_SUMMARY_URL = "https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do"
HKEX_CONNECT_PAGE_URL = (
    "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/"
    "Historical-Daily?sc_lang=zh-CN"
)

SSE_ETF_NAV_CLASSES = (
    # These are the ETF_CLASS values used by the SSE ETF disclosure page's
    # public script.  The page groups them into stock, bond, cross-border,
    # commodity and exchange-traded money-market tabs; querying the values
    # directly keeps the raw responses auditable and avoids relying on tab
    # order.  The F111--F150 values previously used here are not accepted by
    # this endpoint and return an empty result.
    "01",  # single-market Shanghai equity ETF
    "03",  # cross-market equity (Shanghai/Shenzhen/Beijing) ETF
    "08",  # cross-market equity (including Hong Kong) ETF
    "09",  # single-market STAR-market equity ETF
    "31",  # cross-market equity ETF including STAR market
    "02",  # single-market Shanghai bond ETF
    "36",  # cross-market bond ETF (interbank)
    "37",  # cash-substitution bond ETF
    "32",  # cross-market bond (Shanghai/Shenzhen) ETF
    "33",  # cross-border ETF
    "34",  # multi-market cross-border ETF (intraday rotation)
    "35",  # multi-market cross-border ETF (non-intraday rotation)
    "38",  # commodity-futures ETF
    "06",  # gold ETF
    "05",  # exchange-traded money-market fund
    "07",  # exchange-traded money-market fund (legacy subtype)
)


def parse_hkex_tab_data(text: str) -> list[dict[str, Any]]:
    r"""Parse HKEX's JavaScript assignment while retaining the original raw bytes.

    Most daily files contain strict JSON, but some historical files use
    JavaScript-only escapes such as ``\'`` or ``\xNN`` inside stock names.
    """
    normalized = text.lstrip("\ufeff").strip()
    match = re.fullmatch(
        r"tabData\s*=\s*(\[.*\])\s*;?", normalized, flags=re.DOTALL
    )
    if not match:
        raise ValueError("HKEX response did not match the expected tabData JSON wrapper")
    payload = match.group(1)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        payload = payload.replace("\\'", "'")
        payload = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda item: chr(int(item.group(1), 16)),
            payload,
        )
        payload = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", payload)
        parsed = json.loads(payload)
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) for item in parsed
    ):
        raise ValueError("HKEX tabData payload was not an array of objects")
    return parsed


def _number(value: Any, *, multiplier: float = 1.0) -> float | None:
    if value is None or value == "":
        return None
    normalized = str(value).replace(",", "").strip()
    if normalized in {"-", "--", "N/A", "NA"}:
        return None
    return float(normalized) * multiplier


def _parse_bse_jsonp(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the BSE summary endpoint without trusting a callback name.

    The public page currently returns ``null([[{...}], 'YYYY-MM-DD'])``.  It
    is JSONP rather than JSON, and the callback name has changed in the past,
    so only the first JSON value inside the parentheses is accepted.  Any
    trailing callback arguments remain audit evidence but cannot alter the
    normalized row.
    """

    normalized = text.lstrip("\ufeff").strip()
    opening = normalized.find("(")
    if opening < 0:
        raise ValueError("BSE response did not contain a JSONP payload")
    decoder = json.JSONDecoder()
    argument_text = normalized[opening + 1 :]
    payload, end = decoder.raw_decode(argument_text)
    if not isinstance(payload, list) or len(payload) < 1:
        raise ValueError("BSE JSONP payload was not an array")
    rows_payload = payload[0]
    rows = rows_payload if isinstance(rows_payload, list) else []
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("BSE summary rows were not objects")
    reported_date = None
    if len(payload) > 1 and payload[1] not in (None, ""):
        reported_date = _iso_date(payload[1], field="reported_date")
    else:
        # The current endpoint emits the date as a second JavaScript argument
        # after the first JSON value, e.g. `null([[...]], '2026-08-20')`.
        trailing_date = re.search(r"['\"](20\d{2}-\d{2}-\d{2})['\"]", argument_text[end:])
        if trailing_date:
            reported_date = _iso_date(trailing_date.group(1), field="reported_date")
    return rows, reported_date


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _nav(value: Any) -> float | None:
    """Parse SSE's display NAV, including the currency prefix (￥/¥)."""
    if value is None or pd.isna(value):
        return None
    normalized = str(value).replace("￥", "").replace("¥", "").replace(",", "").strip()
    if normalized in {"", "-", "--", "N/A", "NA"}:
        return None
    number = float(normalized)
    return number if pd.notna(number) else None


def _empty_sse_etf_nav_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "trade_date",
            "nav_date",
            "market",
            "instrument_code",
            "instrument_name",
            "nav",
            "source_id",
            "source_grade",
            "date_basis",
            "observed_at",
        ]
    )


def _iso_date(value: Any, *, field: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{field} did not contain a valid date: {value!r}")
    return parsed.strftime("%Y-%m-%d")


def _szse_etf_dates(
    payload: object, known_trading_dates: list[str]
) -> tuple[str, str, str, str | None]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("SZSE ETF metadata response did not contain the expected report")
    metadata = payload[0].get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("SZSE ETF report did not contain metadata")

    page_date = _iso_date(metadata.get("subname"), field="metadata.subname")
    scale_header = str((metadata.get("cols") or {}).get("dqgm", ""))
    displayed_match = re.search(r"(20\d{2}-\d{2}-\d{2})", scale_header)
    displayed_data_date = (
        _iso_date(displayed_match.group(1), field="metadata.cols.dqgm")
        if displayed_match
        else None
    )
    parsed_dates = [
        _iso_date(value, field="known_trading_dates") for value in known_trading_dates
    ]
    prior_dates = sorted(value for value in parsed_dates if value < page_date)
    expected_data_date = prior_dates[-1] if prior_dates else None
    if not expected_data_date:
        raise ValueError(
            "SZSE ETF data date could not be established from the page and known trading dates"
        )
    # The exchange's list page is the disclosure date.  Per the source's
    # publication rule, the downloaded workbook belongs to its preceding
    # trading day.  The date embedded in the column label is display metadata:
    # it can equal the disclosure date, so it must not override that rule.
    return page_date, expected_data_date, scale_header, displayed_data_date


class OfficialMarketAdapters:
    def __init__(self, session: requests.Session, data_root: Path, run_id: str):
        self.session = session
        self.data_root = data_root
        self.run_id = run_id

    @staticmethod
    def _headers(referer: str) -> dict[str, str]:
        return {"Referer": referer}

    def fetch_sse_etf(self, trade_date: str) -> tuple[pd.DataFrame, FetchArtifact]:
        compact_date = trade_date.replace("-", "")
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": trade_date,
        }
        response = get(
            self.session,
            SSE_ETF.url,
            params=params,
            headers=self._headers("https://www.sse.com.cn/assortment/fund/etf/list/scale/"),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=SSE_ETF,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="json",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/json"),
            request_params=params,
        )
        payload = response.json()
        rows = payload.get("result") or []
        normalized = pd.DataFrame(
            [
                {
                    "trade_date": row.get("STAT_DATE") or trade_date,
                    "market": "SSE",
                    "instrument_code": str(row.get("SEC_CODE", "")).zfill(6),
                    "instrument_name": row.get("SEC_NAME"),
                    "etf_type": row.get("ETF_TYPE"),
                    "investment_type": None,
                    "shares": _number(row.get("TOT_VOL"), multiplier=10_000),
                    "nav": None,
                    "date_basis": "source_published",
                    "source_id": SSE_ETF.source_id,
                    "source_grade": SSE_ETF.source_grade,
                    "observed_at": artifact.observed_at,
                }
                for row in rows
            ]
        )
        if compact_date and not normalized.empty:
            normalized["trade_date"] = pd.to_datetime(
                normalized["trade_date"], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
        return self._persist(normalized, artifact, trade_date)

    def fetch_sse_etf_nav(
        self, publication_date: str, known_trading_dates: list[str]
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        """Fetch the SSE ETF list's official unit NAV for one publication day.

        The SSE list is published on the trading morning and its NAV is the
        previous trading day's unit NAV.  The endpoint only exposes the
        current publication list, so a response whose ``TRADING_DAY`` does
        not equal ``publication_date`` is retained as raw evidence but is not
        assigned to the requested share snapshot.
        """
        parsed_dates = sorted(
            _iso_date(value, field="known_trading_dates")
            for value in known_trading_dates
        )
        prior_dates = [value for value in parsed_dates if value < publication_date]
        nav_date = prior_dates[-1] if prior_dates else None
        responses: list[dict[str, Any]] = []
        request_log: list[dict[str, Any]] = []
        warnings: list[str] = []
        for etf_class in SSE_ETF_NAV_CLASSES:
            params = {
                "isPagination": "true",
                "pageHelp.pageSize": "10000",
                "pageHelp.pageNo": "1",
                "pageHelp.beginPage": "1",
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": "1",
                "sqlId": "COMMON_SSE_PL_ETFGGSGSHQD_L",
                "ETF_CLASS": etf_class,
                "type": "inParams",
                "FUND_CODE": "",
                "KEY_WORDS": "",
            }
            try:
                response = get(
                    self.session,
                    SSE_ETF_NAV.url,
                    params=params,
                    headers=self._headers("https://www.sse.com.cn/disclosure/fund/etflist/"),
                )
            except Exception as exc:
                warnings.append(f"ETF_CLASS={etf_class} 请求失败：{type(exc).__name__}")
                continue
            try:
                payload = response.json()
            except (TypeError, ValueError):
                warnings.append(f"ETF_CLASS={etf_class} 返回不是 JSON")
                continue
            rows = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                rows = []
            responses.append(
                {
                    "etf_class": etf_class,
                    "status_code": response.status_code,
                    "result": rows,
                }
            )
            request_log.append(params)

        raw_payload = json.dumps(
            {
                "publication_date_requested": publication_date,
                "nav_date": nav_date,
                "requests": request_log,
                "responses": responses,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        artifact = write_raw_response(
            root=self.data_root,
            source=SSE_ETF_NAV,
            requested_date=publication_date,
            run_id=self.run_id,
            content=raw_payload,
            extension="json",
            status_code=200 if responses else 503,
            content_type="application/json",
            request_params={
                "sqlId": "COMMON_SSE_PL_ETFGGSGSHQD_L",
                "ETF_CLASS": list(SSE_ETF_NAV_CLASSES),
                "publication_date": publication_date,
                "nav_date_rule": "TRADING_DAY_previous_known_trading_day",
            },
        )
        artifact.warnings.extend(warnings)

        normalized_rows: list[dict[str, Any]] = []
        for response_payload in responses:
            for row in response_payload["result"]:
                row_date = row.get("TRADING_DAY") if isinstance(row, dict) else None
                if not row_date:
                    continue
                row_date = _iso_date(row_date, field="TRADING_DAY")
                if row_date != publication_date:
                    continue
                code = str(row.get("FUNDID2") or "").strip().zfill(6)
                value = _nav(row.get("NAV"))
                if not re.fullmatch(r"\d{6}", code) or value is None:
                    continue
                normalized_rows.append(
                    {
                        "trade_date": publication_date,
                        "nav_date": nav_date,
                        "market": "SSE",
                        "instrument_code": code,
                        "instrument_name": row.get("ETF_FULLNAME") or code,
                        "nav": value,
                        "source_id": SSE_ETF_NAV.source_id,
                        "source_grade": SSE_ETF_NAV.source_grade,
                        "date_basis": SSE_ETF_NAV.date_semantics,
                        "observed_at": artifact.observed_at,
                    }
                )
        normalized = pd.DataFrame(normalized_rows)
        if normalized.empty:
            artifact.warnings.append(
                f"上交所未返回 {publication_date} 的可匹配 ETF 单位净值；不把其他公告日净值用于该日。"
            )
            normalized = _empty_sse_etf_nav_frame()
        else:
            normalized = normalized.drop_duplicates(
                ["trade_date", "market", "instrument_code"], keep="last"
            ).reset_index(drop=True)
        return self._persist(normalized, artifact, publication_date)

    def fetch_sse_etf_pcf(
        self, etf_codes: list[str], trade_date: str, *, max_workers: int = 4
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        """Fetch PCFs for ETFs whose shares actually changed.

        SSE publishes the PCF before the *current* trading day.  When the
        collector runs after close, the endpoint has already advanced to the
        next trading day; its ``PRE_TRADING_DAY`` then identifies the share
        snapshot we are measuring.  We accept that explicit relationship and
        preserve both dates in the normalized evidence.  A response that is
        neither the requested trading day nor a PCF whose previous trading day
        matches it is discarded as a date mismatch.  This keeps the estimate
        auditable without adding requests or silently using an unrelated
        basket.
        """

        requested_codes = list(
            dict.fromkeys(
                str(code).strip().zfill(6)
                for code in etf_codes
                if re.fullmatch(r"\d{6}", str(code).strip().zfill(6))
            )
        )
        # A very large changed universe is a source/quality condition, not a
        # reason to silently sample.  Keep the run bounded and expose the
        # omitted count in the artifact warning.
        max_codes = 800
        omitted_count = max(0, len(requested_codes) - max_codes)
        requested_codes = requested_codes[:max_codes]
        request_log: list[dict[str, Any]] = []
        response_log: list[dict[str, Any]] = []
        normalized_rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        aligned_previous_day_count = 0

        def fetch_one(code: str) -> dict[str, Any]:
            basic_params = {
                "isPagination": "false",
                "FUNDID2": code,
                "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C",
            }
            component_params = {
                "isPagination": "false",
                "FUNDID2": code,
                "ETF_VERSION": "XML",
                "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_COMPONENT_C",
            }
            basic_response = get(
                self.session,
                SSE_ETF_PCF.url,
                params=basic_params,
                headers=self._headers(
                    f"https://www.sse.com.cn/disclosure/fund/etflist/detail.shtml?fundid={code}"
                ),
            )
            component_response = get(
                self.session,
                SSE_ETF_PCF.url,
                params=component_params,
                headers=self._headers(
                    f"https://www.sse.com.cn/disclosure/fund/etflist/detail.shtml?fundid={code}"
                ),
            )
            basic_payload = basic_response.json()
            component_payload = component_response.json()
            basic_rows = basic_payload.get("result") if isinstance(basic_payload, dict) else []
            component_rows = component_payload.get("result") if isinstance(component_payload, dict) else []
            if not isinstance(basic_rows, list):
                basic_rows = []
            if not isinstance(component_rows, list):
                component_rows = []
            basic = next(
                (
                    row
                    for row in basic_rows
                    if isinstance(row, dict)
                    and str(row.get("FUNDID2") or row.get("TRADE_CODE") or "").zfill(6) == code
                ),
                basic_rows[0] if basic_rows and isinstance(basic_rows[0], dict) else None,
            )
            observed_date = None
            previous_trading_date = None
            if basic:
                observed_date = (
                    _iso_date(basic.get("TRADING_DAY"), field="TRADING_DAY")
                    if basic.get("TRADING_DAY")
                    else None
                )
                previous_trading_date = (
                    _iso_date(basic.get("PRE_TRADING_DAY"), field="PRE_TRADING_DAY")
                    if basic.get("PRE_TRADING_DAY")
                    else None
                )
            return {
                "code": code,
                "basic": basic,
                "components": component_rows,
                "observed_date": observed_date,
                "previous_trading_date": previous_trading_date,
                "basic_params": basic_params,
                "component_params": component_params,
                "basic_status": basic_response.status_code,
                "component_status": component_response.status_code,
            }

        results: list[dict[str, Any]] = []
        if requested_codes:
            with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as executor:
                futures = {executor.submit(fetch_one, code): code for code in requested_codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        warnings.append(f"{code} PCF 请求失败：{type(exc).__name__}")

        for result in sorted(results, key=lambda item: item["code"]):
            code = result["code"]
            request_log.append(
                {
                    "code": code,
                    "basic": result["basic_params"],
                    "components": result["component_params"],
                    "basic_status": result["basic_status"],
                    "component_status": result["component_status"],
                    "observed_date": result["observed_date"],
                    "previous_trading_date": result["previous_trading_date"],
                }
            )
            response_log.append(
                {
                    "code": code,
                    "basic": result["basic"],
                    "components": result["components"],
                    "observed_date": result["observed_date"],
                    "previous_trading_date": result["previous_trading_date"],
                }
            )
            same_day = result["observed_date"] == trade_date
            previous_day = result["previous_trading_date"] == trade_date
            if not same_day and not previous_day:
                warnings.append(
                    f"{code} PCF 日期 {result['observed_date'] or '缺失'} / 前一交易日 "
                    f"{result['previous_trading_date'] or '缺失'} 与请求日期 {trade_date} 不一致"
                )
                continue
            date_basis = (
                "sse_pcf_same_day_morning"
                if same_day
                else "sse_pcf_next_day_previous_trading_day"
            )
            if previous_day and not same_day:
                aligned_previous_day_count += 1
            basic = result["basic"] or {}
            creation_unit = _integer(
                basic.get("CREATION_REDEMPTION_UNIT")
                or basic.get("CREATION_UNIT")
            )
            if creation_unit is None or creation_unit <= 0:
                warnings.append(f"{code} PCF 缺少有效最小申赎单位")
                continue
            etf_name = basic.get("FUND_NAME") or basic.get("FUND_FULL_NAME") or code
            for row in result["components"]:
                if not isinstance(row, dict):
                    continue
                listing_market = str(
                    row.get("UNDERLYION_SECURITY_ID")
                    or row.get("UNDERLYING_SECURITY_ID")
                    or ""
                ).strip()
                stock_code = normalise_underlying_code(
                    str(
                        row.get("INSTRUMENT_ID")
                        or row.get("STOCK_CODE")
                        or row.get("UNDERLYING_SECURITY_ID")
                        or ""
                    ).strip(),
                    listing_market,
                )
                quantity = _integer(row.get("QUANTITY"))
                if (
                    not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", stock_code)
                    or quantity is None
                    or quantity <= 0
                ):
                    continue
                normalized_row = {
                    "trade_date": trade_date,
                    "pcf_date": result["observed_date"],
                    "market": "SSE",
                    "etf_code": code,
                    "etf_name": etf_name,
                    "stock_code": stock_code,
                    "stock_name": row.get("INSTRUMENT_NAME") or stock_code,
                    "quantity": quantity,
                    "creation_unit": creation_unit,
                    "substitution_flag": row.get("SUBSTITUTION_FLAG") or "",
                    "listing_market": listing_market,
                    "source_id": SSE_ETF_PCF.source_id,
                    "source_grade": SSE_ETF_PCF.source_grade,
                    "date_basis": date_basis,
                }
                if result["previous_trading_date"]:
                    normalized_row["pcf_previous_date"] = result["previous_trading_date"]
                normalized_rows.append(normalized_row)

        if omitted_count:
            warnings.append(f"changed_etf_count_exceeds_cap:{omitted_count}")
        if aligned_previous_day_count:
            warnings.append(
                f"pcf_previous_day_alignment_count:{aligned_previous_day_count}"
            )
        raw_payload = json.dumps(
            {
                "requested_date": trade_date,
                "requested_etf_count": len(etf_codes),
                "captured_etf_count": len(requested_codes),
                "requests": request_log,
                "responses": response_log,
                "warnings": warnings,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # The component response is the raw evidence. Compress only this
        # optional combined artifact so the chunked importer stays below its
        # 12 MB request cap even on a high-turnover day.
        raw_payload = gzip.compress(raw_payload, compresslevel=6)
        artifact = write_raw_response(
            root=self.data_root,
            source=SSE_ETF_PCF,
            requested_date=trade_date,
            run_id=self.run_id,
            content=raw_payload,
            extension="gz",
            status_code=200 if request_log else 503,
            content_type="application/gzip",
            request_params={
                "sqlId": [
                    "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C",
                    "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_COMPONENT_C",
                ],
                "same_day_or_previous_day_required": True,
                "max_codes": max_codes,
            },
        )
        artifact.warnings.extend(warnings)
        normalized = pd.DataFrame(normalized_rows)
        if normalized.empty:
            normalized = pd.DataFrame(
                columns=[
                    "trade_date", "pcf_date", "pcf_previous_date", "market", "etf_code", "etf_name",
                    "stock_code", "stock_name", "quantity", "creation_unit",
                    "substitution_flag", "listing_market", "source_id",
                    "source_grade", "date_basis",
                ]
            )
        else:
            normalized["observed_at"] = artifact.observed_at
        return self._persist(normalized, artifact, trade_date)

    def fetch_szse_etf_current(
        self, known_trading_dates: list[str]
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        metadata_params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": "1000_lf",
            "TABKEY": "tab1",
            "tab1PAGENO": "1",
            "tab1PAGESIZE": "1",
        }
        metadata_response = get(
            self.session,
            SZSE_ETF_METADATA_URL,
            params=metadata_params,
            headers=self._headers(SZSE_ETF_PAGE_URL),
        )
        page_date, trade_date, scale_header, displayed_scale_date = _szse_etf_dates(
            metadata_response.json(), known_trading_dates
        )
        params = {
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1000_lf",
            "TABKEY": "tab1",
        }
        response = get(
            self.session,
            SZSE_ETF.url,
            params=params,
            headers=self._headers(SZSE_ETF_PAGE_URL),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=SZSE_ETF,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="xlsx",
            status_code=response.status_code,
            content_type=response.headers.get(
                "Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            request_params={
                **params,
                "page_metadata_url": SZSE_ETF_METADATA_URL,
                "page_date": page_date,
                "displayed_scale_date": displayed_scale_date,
                "displayed_scale_header": scale_header,
                "date_rule": "page_date_previous_known_trading_day",
            },
        )
        frame = pd.read_excel(
            io.BytesIO(response.content), engine="openpyxl", dtype={"基金代码": str}
        )
        # The SZSE list is a fund-product list, not an ETF-only list: it also
        # contains LOF and REIT rows.  Keep the original XLSX intact for
        # evidence, but never let non-ETF products enter etf_daily or the ETF
        # state.  A missing category column is treated as an explicit warning
        # rather than silently classifying the whole response as ETF.
        product_type = frame.get("基金类别")
        if product_type is None:
            artifact.warnings.append("深交所 ETF 列表缺少基金类别列；未进行产品类型过滤")
        else:
            etf_mask = product_type.astype(str).str.strip().eq("ETF")
            excluded_count = int((~etf_mask).sum())
            if excluded_count:
                artifact.warnings.append(
                    f"深交所列表保留 ETF {int(etf_mask.sum())} 行，排除非 ETF 产品 {excluded_count} 行"
                )
            frame = frame.loc[etf_mask].copy()
        normalized = pd.DataFrame(
            {
                "trade_date": trade_date,
                "market": "SZSE",
                "instrument_code": frame["基金代码"].astype(str).str.zfill(6),
                "instrument_name": frame["基金简称"],
                "etf_type": frame.get("基金类别"),
                "investment_type": frame.get("投资类别"),
                "shares": pd.to_numeric(
                    frame["当前规模(份)"].astype(str).str.replace(",", "", regex=False),
                    errors="coerce",
                ),
                "nav": pd.to_numeric(frame.get("净值"), errors="coerce"),
                "nav_date": trade_date,
                "date_basis": "source_page_previous_trading_day",
                "source_page_date": page_date,
                "source_scale_date": trade_date,
                "source_id": SZSE_ETF.source_id,
                "source_grade": SZSE_ETF.source_grade,
                "observed_at": artifact.observed_at,
            }
        )
        return self._persist(normalized, artifact, trade_date)

    def fetch_sse_margin(
        self, start_date: str, end_date: str
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        params = {
            "isPagination": "true",
            "beginDate": start_date.replace("-", ""),
            "endDate": end_date.replace("-", ""),
            "tabType": "",
            "stockCode": "",
            "pageHelp.pageSize": "5000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "5",
        }
        response = get(
            self.session,
            SSE_MARGIN.url,
            params=params,
            headers=self._headers("https://www.sse.com.cn/market/othersdata/margin/sum/"),
        )
        partition = f"{start_date}_{end_date}"
        artifact = write_raw_response(
            root=self.data_root,
            source=SSE_MARGIN,
            requested_date=partition,
            run_id=self.run_id,
            content=response.content,
            extension="json",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/json"),
            request_params=params,
        )
        rows = response.json().get("result") or []
        normalized = pd.DataFrame(
            [
                {
                    "trade_date": pd.to_datetime(row.get("opDate"), format="%Y%m%d").strftime(
                        "%Y-%m-%d"
                    ),
                    "market": "SSE",
                    "financing_balance_yuan": _number(row.get("rzye")),
                    "financing_buy_yuan": _number(row.get("rzmre")),
                    "securities_lending_balance_yuan": _number(row.get("rqylje")),
                    "securities_lending_volume_shares": _number(row.get("rqyl")),
                    "securities_lending_sell_volume_shares": _number(row.get("rqmcl")),
                    "margin_total_balance_yuan": _number(row.get("rzrqjyzl")),
                    "source_id": SSE_MARGIN.source_id,
                    "source_grade": SSE_MARGIN.source_grade,
                    "observed_at": artifact.observed_at,
                }
                for row in rows
            ]
        )
        return self._persist(normalized, artifact, partition)

    def fetch_szse_margin(self, trade_date: str) -> tuple[pd.DataFrame, FetchArtifact]:
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": "1837_xxpl",
            "txtDate": trade_date,
            "tab1PAGENO": "1",
        }
        response = get(
            self.session,
            SZSE_MARGIN.url,
            params=params,
            headers=self._headers("https://www.szse.cn/disclosure/margin/margin/index.html"),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=SZSE_MARGIN,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="json",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/json"),
            request_params=params,
        )
        payload = response.json()
        rows = payload[0].get("data", []) if payload else []
        normalized = pd.DataFrame(
            [
                {
                    "trade_date": trade_date,
                    "market": "SZSE",
                    "financing_balance_yuan": _number(row.get("jrrzye"), multiplier=100_000_000),
                    "financing_buy_yuan": _number(row.get("jrrzmr"), multiplier=100_000_000),
                    "securities_lending_balance_yuan": _number(
                        row.get("jrrjye"), multiplier=100_000_000
                    ),
                    "securities_lending_volume_shares": _number(
                        row.get("jrrjyl"), multiplier=100_000_000
                    ),
                    "securities_lending_sell_volume_shares": _number(
                        row.get("jrrjmc"), multiplier=100_000_000
                    ),
                    "margin_total_balance_yuan": _number(
                        row.get("jrrzrjye"), multiplier=100_000_000
                    ),
                    "source_id": SZSE_MARGIN.source_id,
                    "source_grade": SZSE_MARGIN.source_grade,
                    "observed_at": artifact.observed_at,
                }
                for row in rows
            ]
        )
        if normalized.empty:
            artifact.warnings.append(f"深交所尚未返回 {trade_date} 的两融汇总数据。")
        return self._persist(normalized, artifact, trade_date)

    def fetch_bse_margin(self, trade_date: str) -> tuple[pd.DataFrame, FetchArtifact]:
        """Fetch the BSE's official previous-trading-day margin summary.

        BSE exposes the public table through a JSONP endpoint.  The raw
        response is retained verbatim; only a response whose explicit
        reported date equals ``trade_date`` is normalized.  This prevents a
        successful HTTP 200 for the latest available date from being assigned
        to an older requested date.
        """

        params = {"transDate": trade_date, "page": "0"}
        response = get(
            self.session,
            BSE_MARGIN_SUMMARY_URL,
            params=params,
            headers=self._headers(BSE_MARGIN_PAGE_URL),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=BSE_MARGIN,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="jsonp",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/javascript"),
            request_params={
                **params,
                "page_url": BSE_MARGIN_PAGE_URL,
                "date_rule": "explicit_reported_date_must_equal_requested_date",
                "units": {
                    "rzmre_rzye_rqye_rzrqye": "yuan",
                    "rqmcl_rqyl": "shares",
                },
            },
        )

        rows, reported_date = _parse_bse_jsonp(response.text)
        normalized_rows: list[dict[str, Any]] = []
        if reported_date != trade_date:
            artifact.warnings.append(
                f"北交所返回日期 {reported_date or '缺失'}，未匹配请求日期 {trade_date}。"
            )
        elif rows:
            row = rows[0]
            normalized_rows.append(
                {
                    "trade_date": reported_date,
                    "market": "BSE",
                    "financing_balance_yuan": _number(row.get("rzye")),
                    "financing_buy_yuan": _number(row.get("rzmre")),
                    "securities_lending_balance_yuan": _number(row.get("rqye")),
                    "securities_lending_volume_shares": _number(row.get("rqyl")),
                    "securities_lending_sell_volume_shares": _number(row.get("rqmcl")),
                    "margin_total_balance_yuan": _number(row.get("rzrqye")),
                    "source_id": BSE_MARGIN.source_id,
                    "source_grade": BSE_MARGIN.source_grade,
                    "date_basis": BSE_MARGIN.date_semantics,
                    "observed_at": artifact.observed_at,
                }
            )
        else:
            artifact.warnings.append(f"北交所未返回 {trade_date} 的两融汇总数据。")

        normalized = pd.DataFrame(normalized_rows)
        if normalized.empty:
            normalized = pd.DataFrame(
                columns=[
                    "trade_date",
                    "market",
                    "financing_balance_yuan",
                    "financing_buy_yuan",
                    "securities_lending_balance_yuan",
                    "securities_lending_volume_shares",
                    "securities_lending_sell_volume_shares",
                    "margin_total_balance_yuan",
                    "source_id",
                    "source_grade",
                    "date_basis",
                    "observed_at",
                ]
            )
        return self._persist(normalized, artifact, trade_date)

    def fetch_hkex_connect(
        self, trade_date: str
    ) -> tuple[pd.DataFrame, pd.DataFrame, FetchArtifact]:
        compact_date = trade_date.replace("-", "")
        url = (
            "https://www.hkex.com.hk/chi/csm/DailyStat/"
            f"data_tab_daily_{compact_date}c.js"
        )
        try:
            response = get(
                self.session,
                url,
                headers=self._headers(HKEX_CONNECT_PAGE_URL),
            )
        except requests.HTTPError as exc:
            response = exc.response
            if response is None or response.status_code != 404:
                raise
            artifact = write_raw_response(
                root=self.data_root,
                source=HKEX_CONNECT,
                requested_date=trade_date,
                run_id=self.run_id,
                content=response.content,
                extension="html",
                status_code=response.status_code,
                content_type=response.headers.get("Content-Type", "text/html"),
                request_params={
                    "date": trade_date,
                    "language": "zh-CN",
                    "page_url": HKEX_CONNECT_PAGE_URL,
                    "availability": "official_daily_file_not_retained",
                },
            )
            summary = pd.DataFrame(
                columns=[
                    "trade_date",
                    "market",
                    "direction",
                    "currency",
                    "total_turnover",
                    "buy_turnover",
                    "sell_turnover",
                    "net_buy",
                    "total_trade_count",
                    "buy_trade_count",
                    "sell_trade_count",
                    "etf_turnover",
                    "daily_quota_balance",
                    "source_id",
                    "source_grade",
                    "observed_at",
                ]
            )
            top = pd.DataFrame(
                columns=[
                    "trade_date",
                    "market",
                    "direction",
                    "currency",
                    "rank",
                    "instrument_code",
                    "instrument_name",
                    "instrument_name_raw",
                    "instrument_name_source",
                    "buy_turnover",
                    "sell_turnover",
                    "total_turnover",
                    "source_id",
                    "source_grade",
                    "observed_at",
                ]
            )
            summary_path = write_normalized_csv(
                root=self.data_root,
                dataset="hkex_stock_connect_summary",
                partition=trade_date,
                run_id=self.run_id,
                frame=summary,
            )
            top_path = write_normalized_csv(
                root=self.data_root,
                dataset="hkex_stock_connect_top",
                partition=trade_date,
                run_id=self.run_id,
                frame=top,
            )
            artifact.normalized_path = f"{summary_path};{top_path}"
            artifact.warnings.append(
                "港交所中文历史页仅保留近期每日文件；该日期官方文件返回 404。"
            )
            return summary, top, artifact
        artifact = write_raw_response(
            root=self.data_root,
            source=HKEX_CONNECT,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="js",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/javascript"),
            request_params={
                "date": trade_date,
                "language": "zh-CN",
                "page_url": HKEX_CONNECT_PAGE_URL,
            },
        )
        markets = parse_hkex_tab_data(response.text)
        summary_rows: list[dict[str, Any]] = []
        top_rows: list[dict[str, Any]] = []
        for market_payload in markets:
            if not market_payload.get("tradingDay"):
                continue
            market_label = str(market_payload.get("market", ""))
            exchange = "SSE" if market_label.startswith("SSE") else "SZSE"
            direction = "northbound" if "Northbound" in market_label else "southbound"
            currency = "RMB" if direction == "northbound" else "HKD"
            for content in market_payload.get("content", []):
                table = content.get("table", {})
                schema = (table.get("schema") or [[]])[0]
                if content.get("style") == 1:
                    values = [
                        row.get("td", [[None]])[0][0] for row in table.get("tr", [])
                    ]
                    raw = dict(zip(schema, values))
                    dqb = _number(raw.get("DQB"))
                    if dqb is not None and dqb >= 999_999_999:
                        dqb = None
                    buy = _number(raw.get("Buy Turnover"), multiplier=1_000_000)
                    sell = _number(raw.get("Sell Turnover"), multiplier=1_000_000)
                    summary_rows.append(
                        {
                            "trade_date": market_payload.get("date") or trade_date,
                            "market": exchange,
                            "direction": direction,
                            "currency": currency,
                            "total_turnover": _number(
                                raw.get("Total Turnover"), multiplier=1_000_000
                            ),
                            "buy_turnover": buy,
                            "sell_turnover": sell,
                            "net_buy": None if buy is None or sell is None else buy - sell,
                            "total_trade_count": _integer(raw.get("Total Trade Count")),
                            "buy_trade_count": _integer(raw.get("Buy Trade Count")),
                            "sell_trade_count": _integer(raw.get("Sell Trade Count")),
                            "etf_turnover": _number(
                                raw.get("ETF Turnover"), multiplier=1_000_000
                            ),
                            "daily_quota_balance": dqb,
                            "source_id": HKEX_CONNECT.source_id,
                            "source_grade": HKEX_CONNECT.source_grade,
                            "observed_at": artifact.observed_at,
                        }
                    )
                elif content.get("style") == 2:
                    for row in table.get("tr", []):
                        values = row.get("td", [[]])[0]
                        raw = dict(zip(schema, values))
                        instrument_code = (
                            str(raw.get("Stock Code", "")).zfill(6)
                            if direction == "northbound"
                            else str(raw.get("Stock Code", "")).zfill(5)
                        )
                        raw_name = clean_instrument_name(raw.get("Stock Name"))
                        instrument_name, instrument_name_source = (
                            resolve_hkex_instrument_name(
                                code=instrument_code,
                                direction=direction,
                                raw_name=raw_name,
                            )
                        )
                        top_rows.append(
                            {
                                "trade_date": market_payload.get("date") or trade_date,
                                "market": exchange,
                                "direction": direction,
                                "currency": currency,
                                "rank": _integer(raw.get("Rank")),
                                "instrument_code": instrument_code,
                                "instrument_name": instrument_name,
                                "instrument_name_raw": raw_name,
                                "instrument_name_source": instrument_name_source,
                                "buy_turnover": _number(raw.get("Buy Turnover")),
                                "sell_turnover": _number(raw.get("Sell Turnover")),
                                "total_turnover": _number(raw.get("Total Turnover")),
                                "source_id": HKEX_CONNECT.source_id,
                                "source_grade": HKEX_CONNECT.source_grade,
                                "observed_at": artifact.observed_at,
                            }
                        )
        summary = pd.DataFrame(summary_rows)
        top = pd.DataFrame(top_rows)
        summary_path = write_normalized_csv(
            root=self.data_root,
            dataset="hkex_stock_connect_summary",
            partition=trade_date,
            run_id=self.run_id,
            frame=summary,
        )
        top_path = write_normalized_csv(
            root=self.data_root,
            dataset="hkex_stock_connect_top",
            partition=trade_date,
            run_id=self.run_id,
            frame=top,
        )
        artifact.row_count = len(summary) + len(top)
        artifact.normalized_path = f"{summary_path};{top_path}"
        return summary, top, artifact

    def _persist(
        self, frame: pd.DataFrame, artifact: FetchArtifact, partition: str
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        output_path = write_normalized_csv(
            root=self.data_root,
            # Include the publisher in the storage key. SSE and SZSE both expose
            # datasets named ``etf_scale``; without this namespace, same-run
            # snapshots for the same date overwrite one another and destroy the
            # raw -> normalized audit trail.
            dataset=f"{artifact.source.source_id}_{artifact.source.dataset}",
            partition=partition,
            run_id=self.run_id,
            frame=frame,
        )
        artifact.row_count = len(frame)
        artifact.normalized_path = str(output_path)
        return frame, artifact
