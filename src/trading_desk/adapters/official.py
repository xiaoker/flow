from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from ..http import get
from ..models import FetchArtifact, SourceSpec
from ..storage import write_normalized_csv, write_raw_response


SSE_ETF = SourceSpec(
    source_id="sse",
    publisher="上海证券交易所",
    dataset="etf_scale",
    url="https://query.sse.com.cn/commonQuery.do",
)
SZSE_ETF = SourceSpec(
    source_id="szse",
    publisher="深圳证券交易所",
    dataset="etf_scale",
    url="https://fund.szse.cn/api/report/ShowReport",
    date_semantics="collector_inferred_current_snapshot",
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
HKEX_CONNECT = SourceSpec(
    source_id="hkex",
    publisher="香港交易所",
    dataset="stock_connect_daily",
    url="https://www.hkex.com.hk/eng/csm/DailyStat/data_tab_daily_[YYYY][MM][DD]e.js",
)


def _number(value: Any, *, multiplier: float = 1.0) -> float | None:
    if value is None or value == "":
        return None
    normalized = str(value).replace(",", "").strip()
    if normalized in {"-", "--", "N/A", "NA"}:
        return None
    return float(normalized) * multiplier


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


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

    def fetch_szse_etf_current(
        self, inferred_trade_date: str
    ) -> tuple[pd.DataFrame, FetchArtifact]:
        params = {
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1000_lf",
            "TABKEY": "tab1",
        }
        response = get(
            self.session,
            SZSE_ETF.url,
            params=params,
            headers=self._headers("https://fund.szse.cn/marketdata/fundslist/index.html"),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=SZSE_ETF,
            requested_date=None,
            run_id=self.run_id,
            content=response.content,
            extension="xlsx",
            status_code=response.status_code,
            content_type=response.headers.get(
                "Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            request_params=params,
        )
        frame = pd.read_excel(
            io.BytesIO(response.content), engine="openpyxl", dtype={"基金代码": str}
        )
        normalized = pd.DataFrame(
            {
                "trade_date": inferred_trade_date,
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
                "date_basis": "collector_inferred_current_snapshot",
                "source_id": SZSE_ETF.source_id,
                "source_grade": SZSE_ETF.source_grade,
                "observed_at": artifact.observed_at,
            }
        )
        artifact.warnings.append(
            "深交所 ETF 官方下载只提供当前快照且未包含统计日期；trade_date 来自采集任务预期日期，不能用于历史回填。"
        )
        return self._persist(normalized, artifact, inferred_trade_date)

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

    def fetch_hkex_connect(
        self, trade_date: str
    ) -> tuple[pd.DataFrame, pd.DataFrame, FetchArtifact]:
        compact_date = trade_date.replace("-", "")
        url = (
            "https://www.hkex.com.hk/eng/csm/DailyStat/"
            f"data_tab_daily_{compact_date}e.js"
        )
        response = get(
            self.session,
            url,
            headers=self._headers(
                "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily?sc_lang=en"
            ),
        )
        artifact = write_raw_response(
            root=self.data_root,
            source=HKEX_CONNECT,
            requested_date=trade_date,
            run_id=self.run_id,
            content=response.content,
            extension="js",
            status_code=response.status_code,
            content_type=response.headers.get("Content-Type", "application/javascript"),
            request_params={"date": trade_date},
        )
        text = response.text.strip()
        match = re.fullmatch(r"tabData\s*=\s*(\[.*\])\s*;?", text, flags=re.DOTALL)
        if not match:
            raise ValueError("HKEX response did not match the expected tabData JSON wrapper")
        markets = json.loads(match.group(1))
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
                        top_rows.append(
                            {
                                "trade_date": market_payload.get("date") or trade_date,
                                "market": exchange,
                                "direction": direction,
                                "currency": currency,
                                "rank": _integer(raw.get("Rank")),
                                "instrument_code": str(raw.get("Stock Code", "")),
                                "instrument_name": raw.get("Stock Name"),
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
            dataset="stock_connect_summary",
            partition=trade_date,
            run_id=self.run_id,
            frame=summary,
        )
        top_path = write_normalized_csv(
            root=self.data_root,
            dataset="stock_connect_top",
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
            dataset=artifact.source.dataset,
            partition=partition,
            run_id=self.run_id,
            frame=frame,
        )
        artifact.row_count = len(frame)
        artifact.normalized_path = str(output_path)
        return frame, artifact

