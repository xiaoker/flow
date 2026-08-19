from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.adapters import official
from trading_desk.adapters.official import OfficialMarketAdapters


class FakeResponse:
    def __init__(self, payload: object, content_type: str = "application/json"):
        if isinstance(payload, str):
            self.text = payload
            self.content = payload.encode("utf-8")
            self._payload = None
        else:
            self._payload = payload
            self.text = json.dumps(payload)
            self.content = self.text.encode("utf-8")
        self.status_code = 200
        self.headers = {"Content-Type": content_type}

    def json(self) -> object:
        if self._payload is None:
            return json.loads(self.text)
        return self._payload


class BinaryResponse:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.text = ""
        self.status_code = 200
        self.headers = {"Content-Type": content_type}


class MissingResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__("official file not retained", "text/html")
        self.status_code = 404


@pytest.fixture
def adapters(tmp_path: Path) -> OfficialMarketAdapters:
    return OfficialMarketAdapters(session=object(), data_root=tmp_path, run_id="test-run")  # type: ignore[arg-type]


def test_szse_margin_converts_hundred_million_units(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = [
        {
            "metadata": {"subname": "2026-07-14"},
            "data": [
                {
                    "jrrzmr": "1,174.91",
                    "jrrzye": "14,172.64",
                    "jrrjmc": "0.27",
                    "jrrjyl": "8.80",
                    "jrrjye": "74.36",
                    "jrrzrjye": "14,247.00",
                }
            ],
        }
    ]
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_szse_margin("2026-07-14")

    assert artifact.row_count == 1
    assert frame.loc[0, "financing_balance_yuan"] == pytest.approx(1_417_264_000_000)
    assert frame.loc[0, "securities_lending_volume_shares"] == pytest.approx(880_000_000)
    assert frame.loc[0, "margin_total_balance_yuan"] == pytest.approx(1_424_700_000_000)


def test_bse_margin_parses_official_jsonp_and_keeps_canonical_units(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = (
        "null([[{\"rqmcl\":24200,\"rqye\":454583,\"rqyl\":61368,"
        "\"rzmre\":433272098,\"rzrqye\":8211084695,\"rzye\":8210630112,"
        "\"zqdm\":\"999999\"}]], '2026-07-30')"
    )
    captured: dict[str, object] = {}

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(payload)

    monkeypatch.setattr(official, "get", fake_get)

    frame, artifact = adapters.fetch_bse_margin("2026-07-30")

    assert artifact.row_count == 1
    assert frame.loc[0, "trade_date"] == "2026-07-30"
    assert frame.loc[0, "market"] == "BSE"
    assert frame.loc[0, "financing_balance_yuan"] == pytest.approx(8_210_630_112)
    assert frame.loc[0, "financing_buy_yuan"] == pytest.approx(433_272_098)
    assert frame.loc[0, "securities_lending_balance_yuan"] == pytest.approx(454_583)
    assert frame.loc[0, "securities_lending_volume_shares"] == pytest.approx(61_368)
    assert frame.loc[0, "margin_total_balance_yuan"] == pytest.approx(8_211_084_695)
    assert frame.loc[0, "source_trade_date"] == "2026-07-30"
    assert captured["params"] == {
        "callback": "cb",
        "transDate": "2026-07-30",
        "page": "0",
    }


def test_bse_margin_accepts_current_round_fields_without_using_display_units(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = (
        "cb([[{\"rqmcl\":20400,\"rqmclRound\":\"2.04\",\"rqye\":74256,"
        "\"rqyeRound\":\"7.43\",\"rqyl\":21500,\"rqylRound\":\"2.15\","
        "\"rzmre\":634607986,\"rzmreRound\":\"63460.80\","
        "\"rzrqye\":8377084099,\"rzrqyeRound\":\"837708.41\","
        "\"rzye\":8377009843,\"rzyeRound\":\"837700.98\","
        "\"zqdm\":\"999999\"}]], '2026-08-17')"
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_bse_margin("2026-08-17")

    assert artifact.row_count == 1
    assert frame.loc[0, "financing_balance_yuan"] == pytest.approx(8_377_009_843)
    assert frame.loc[0, "financing_buy_yuan"] == pytest.approx(634_607_986)
    assert frame.loc[0, "securities_lending_balance_yuan"] == pytest.approx(74_256)
    assert frame.loc[0, "securities_lending_volume_shares"] == pytest.approx(21_500)
    assert frame.loc[0, "margin_total_balance_yuan"] == pytest.approx(8_377_084_099)


def test_bse_margin_parses_current_bseinfo_wrapper_and_headers(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    # bseinfo.net currently wraps the rows and selected date in one array,
    # unlike the legacy null([[...]], date) response shape.
    payload = (
        "cb([[{\"rqmcl\":20400,\"rqye\":74256,\"rqyl\":21500,"
        "\"rzmre\":634607986,\"rzrqye\":8377084099,\"rzye\":8377009843,"
        "\"zqdm\":\"999999\"}], '2026-08-17'])"
    )
    captured: dict[str, object] = {}

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        captured["url"] = args[1]
        captured["headers"] = kwargs["headers"]
        return FakeResponse(payload)

    monkeypatch.setattr(official, "get", fake_get)

    frame, artifact = adapters.fetch_bse_margin("2026-08-17")

    assert artifact.row_count == 1
    assert frame.loc[0, "financing_balance_yuan"] == pytest.approx(8_377_009_843)
    assert captured["url"] == "https://www.bseinfo.net/rzrqjyyexxController/summaryInfoResult.do"
    assert captured["headers"] == {
        "Referer": "https://www.bseinfo.net/disclosure/rzrq_trans_list.html",
        "Accept": "application/json,text/javascript,*/*;q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }


def test_bse_margin_rejects_stale_jsonp_date(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = "null([[{\"rzye\":1,\"rzrqye\":1,\"zqdm\":\"999999\"}]], '2026-07-29')"
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_bse_margin("2026-07-30")

    assert frame.empty
    assert any("返回日期" in warning for warning in artifact.warnings)


def test_bse_margin_does_not_promote_detail_row_without_official_aggregate(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = "null([[{\"rzye\":1,\"rzrqye\":1,\"zqdm\":\"000001\"}]], '2026-07-30')"
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_bse_margin("2026-07-30")

    assert frame.empty
    assert any("汇总数据" in warning for warning in artifact.warnings)


def test_sse_etf_matches_code_name_date_and_ten_thousand_share_unit(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = {
        "result": [
            {
                "STAT_DATE": "2026-07-15",
                "SEC_CODE": "510300",
                "SEC_NAME": "300ETF",
                "ETF_TYPE": "股票ETF",
                "TOT_VOL": "987,654.32",
            }
        ]
    }
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_sse_etf("2026-07-15")

    assert frame.loc[0, "trade_date"] == "2026-07-15"
    assert frame.loc[0, "market"] == "SSE"
    assert frame.loc[0, "instrument_code"] == "510300"
    assert frame.loc[0, "instrument_name"] == "300ETF"
    assert frame.loc[0, "shares"] == pytest.approx(9_876_543_200)
    assert "sse_etf_scale" in str(artifact.normalized_path)


def test_sse_etf_nav_aligns_exact_publication_to_share_date(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = {
        "result": [
            {
                "TRADING_DAY": "20260715",
                "FUNDID2": "510300",
                "ETF_FULLNAME": "沪深300ETF",
                "NAV": "￥4.3210",
            },
            {
                "TRADING_DAY": "20260714",
                "FUNDID2": "510300",
                "ETF_FULLNAME": "沪深300ETF",
                "NAV": "4.0000",
            },
        ]
    }
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_sse_etf_nav(
        "2026-07-15", ["2026-07-14", "2026-07-15"]
    )

    assert len(frame) == 1
    assert frame.loc[0, "trade_date"] == "2026-07-15"
    assert frame.loc[0, "nav_date"] == "2026-07-15"
    assert frame.loc[0, "source_publication_date"] == "2026-07-15"
    assert frame.loc[0, "source_nav_date"] == "2026-07-14"
    assert frame.loc[0, "instrument_code"] == "510300"
    assert frame.loc[0, "nav"] == pytest.approx(4.321)
    assert frame.loc[0, "date_basis"] == (
        "source_publication_previous_trading_day_nav_aligned_to_share_date_exact_publication"
    )
    assert "sse_etf_nav" in str(artifact.normalized_path)


def test_sse_etf_nav_aligns_next_publication_to_latest_share_date(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = {
        "result": [
            {
                "TRADING_DAY": "20260819",
                "FUNDID2": "510300",
                "ETF_FULLNAME": "沪深300ETF",
                "NAV": "￥4.3210",
            }
        ]
    }
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_sse_etf_nav(
        "2026-08-18", ["2026-08-14", "2026-08-17", "2026-08-18"]
    )

    assert len(frame) == 1
    assert frame.loc[0, "trade_date"] == "2026-08-18"
    assert frame.loc[0, "nav_date"] == "2026-08-18"
    assert frame.loc[0, "source_publication_date"] == "2026-08-19"
    assert frame.loc[0, "source_nav_date"] == "2026-08-18"
    assert frame.loc[0, "date_basis"].endswith("_next_publication")
    assert any("次日公告" in warning for warning in artifact.warnings)


def test_sse_etf_nav_does_not_backfill_older_share_date_from_moving_current_list(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = {
        "result": [
            {
                "TRADING_DAY": "20260819",
                "FUNDID2": "510300",
                "ETF_FULLNAME": "沪深300ETF",
                "NAV": "￥4.3210",
            }
        ]
    }
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, artifact = adapters.fetch_sse_etf_nav(
        "2026-08-17", ["2026-08-14", "2026-08-17", "2026-08-18"]
    )

    assert frame.empty
    assert any("可对齐 2026-08-17" in warning for warning in artifact.warnings)


def test_sse_etf_nav_uses_live_public_page_class_codes() -> None:
    assert official.SSE_ETF_NAV_CLASSES == (
        "01",
        "03",
        "08",
        "09",
        "31",
        "02",
        "36",
        "37",
        "32",
        "33",
        "34",
        "35",
        "38",
        "06",
        "05",
        "07",
    )


def test_sse_etf_pcf_requires_same_day_and_normalizes_components(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    responses = iter(
        [
            FakeResponse(
                {
                    "result": [
                        {
                            "TRADING_DAY": "20260715",
                            "FUNDID2": "588000",
                            "FUND_NAME": "科创50ETF",
                            "CREATION_REDEMPTION_UNIT": "3000000",
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "result": [
                        {
                            "INSTRUMENT_ID": "688008",
                            "INSTRUMENT_NAME": "澜起科技",
                            "QUANTITY": "1813",
                            "SUBSTITUTION_FLAG": "1",
                        }
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: next(responses))

    frame, artifact = adapters.fetch_sse_etf_pcf(["588000"], "2026-07-15")

    assert len(frame) == 1
    assert frame.loc[0, "etf_code"] == "588000"
    assert frame.loc[0, "stock_code"] == "688008"
    assert frame.loc[0, "creation_unit"] == 3_000_000
    assert frame.loc[0, "quantity"] == 1813
    assert frame.loc[0, "date_basis"] == "sse_pcf_same_day_morning"
    assert "现金" not in artifact.warnings


def test_sse_etf_pcf_aligns_next_day_publication_to_pre_trading_day(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    responses = iter(
        [
            FakeResponse(
                {
                    "result": [
                        {
                            "TRADING_DAY": "20260716",
                            "PRE_TRADING_DAY": "20260715",
                            "FUNDID2": "588000",
                            "FUND_NAME": "科创50ETF",
                            "CREATION_REDEMPTION_UNIT": "3000000",
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "result": [
                        {
                            "INSTRUMENT_ID": "688008",
                            "INSTRUMENT_NAME": "澜起科技",
                            "QUANTITY": "1813",
                            "SUBSTITUTION_FLAG": "1",
                        }
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: next(responses))

    frame, artifact = adapters.fetch_sse_etf_pcf(["588000"], "2026-07-15")

    assert len(frame) == 1
    assert frame.loc[0, "trade_date"] == "2026-07-15"
    assert frame.loc[0, "pcf_date"] == "2026-07-16"
    assert frame.loc[0, "pcf_previous_date"] == "2026-07-15"
    assert frame.loc[0, "date_basis"] == "sse_pcf_next_day_previous_trading_day"
    assert "pcf_previous_day_alignment_count:1" in artifact.warnings


def test_sse_etf_pcf_preserves_hk_code_width_and_listing_market(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    responses = iter(
        [
            FakeResponse(
                {
                    "result": [
                        {
                            "TRADING_DAY": "20260715",
                            "FUNDID2": "517180",
                            "FUND_NAME": "国企开放共赢ETF",
                            "CREATION_REDEMPTION_UNIT": "1000000",
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "result": [
                        {
                            "INSTRUMENT_ID": "000883",
                            "INSTRUMENT_NAME": "湖北能源",
                            "UNDERLYION_SECURITY_ID": "102",
                            "QUANTITY": "300",
                        },
                        {
                            "INSTRUMENT_ID": "00883",
                            "INSTRUMENT_NAME": "中海油",
                            "UNDERLYION_SECURITY_ID": "103",
                            "QUANTITY": "1397",
                        },
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: next(responses))

    frame, _ = adapters.fetch_sse_etf_pcf(["517180"], "2026-07-15")

    assert set(frame["stock_code"]) == {"000883", "00883"}
    assert set(frame["listing_market"]) == {"102", "103"}


def test_szse_etf_matches_named_excel_columns_without_row_order_join(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    source = pd.DataFrame(
        [
            {
                "基金代码": "159001",
                "基金简称": "货币ETF易方达",
                "基金类别": "ETF",
                "投资类别": "货币市场基金",
                "当前规模(份)": "19,042,471",
                "净值": 100.0,
            }
        ]
    )
    content = io.BytesIO()
    with pd.ExcelWriter(content, engine="openpyxl") as writer:
        source.to_excel(writer, index=False)
    responses = iter(
        [
            FakeResponse(
                [
                    {
                        "metadata": {
                            "subname": "2026-07-16",
                            "cols": {"dqgm": "当前规模（份）（2026-07-15）"},
                        }
                    }
                ]
            ),
            BinaryResponse(
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ]
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: next(responses))

    frame, artifact = adapters.fetch_szse_etf_current(
        ["2026-07-13", "2026-07-14", "2026-07-15"]
    )

    assert frame.loc[0, "trade_date"] == "2026-07-15"
    assert frame.loc[0, "market"] == "SZSE"
    assert frame.loc[0, "instrument_code"] == "159001"
    assert frame.loc[0, "instrument_name"] == "货币ETF易方达"
    assert frame.loc[0, "shares"] == 19_042_471
    assert frame.loc[0, "date_basis"] == "source_page_previous_trading_day"
    assert frame.loc[0, "source_page_date"] == "2026-07-16"
    assert artifact.requested_date == "2026-07-15"
    assert "szse_etf_scale" in str(artifact.normalized_path)


def test_szse_etf_excludes_non_etf_products_but_preserves_raw_artifact(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    source = pd.DataFrame(
        [
            {
                "基金代码": "159001",
                "基金简称": "货币ETF易方达",
                "基金类别": "ETF",
                "投资类别": "货币市场基金",
                "当前规模(份)": "19,042,471",
                "净值": 100.0,
            },
            {
                "基金代码": "160001",
                "基金简称": "示例LOF",
                "基金类别": "LOF",
                "投资类别": "股票型",
                "当前规模(份)": "10,000,000",
                "净值": 1.0,
            },
        ]
    )
    content = io.BytesIO()
    with pd.ExcelWriter(content, engine="openpyxl") as writer:
        source.to_excel(writer, index=False)
    responses = iter(
        [
            FakeResponse(
                [
                    {
                        "metadata": {
                            "subname": "2026-07-16",
                            "cols": {"dqgm": "当前规模（份）（2026-07-15）"},
                        }
                    }
                ]
            ),
            BinaryResponse(
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ]
    )
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: next(responses))

    frame, artifact = adapters.fetch_szse_etf_current(
        ["2026-07-13", "2026-07-14", "2026-07-15"]
    )

    assert list(frame["instrument_code"]) == ["159001"]
    assert artifact.row_count == 1
    assert any("排除非 ETF 产品 1 行" in warning for warning in artifact.warnings)
    assert Path(artifact.raw_path).exists()


def test_szse_etf_uses_previous_trading_day_when_header_shows_disclosure_date() -> None:
    payload = [
        {
            "metadata": {
                "subname": "2026-07-31",
                "cols": {"dqgm": "当前规模（份）（2026-07-31）"},
            }
        }
    ]

    page_date, trade_date, header, displayed_date = official._szse_etf_dates(
        payload, ["2026-07-29", "2026-07-30", "2026-07-31"]
    )

    assert page_date == "2026-07-31"
    assert trade_date == "2026-07-30"
    assert header == "当前规模（份）（2026-07-31）"
    assert displayed_date == "2026-07-31"


def test_sse_margin_uses_named_source_fields(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = {
        "result": [
            {
                "opDate": "20260714",
                "rqmcl": 60_832_165,
                "rqyl": 2_467_919_946,
                "rqylje": 13_885_066_040,
                "rzmre": 123_363_539_218,
                "rzrqjyzl": 1_465_899_251_954,
                "rzye": 1_452_014_185_914,
            }
        ]
    }
    monkeypatch.setattr(official, "get", lambda *args, **kwargs: FakeResponse(payload))

    frame, _ = adapters.fetch_sse_margin("2026-07-14", "2026-07-14")

    assert frame.loc[0, "trade_date"] == "2026-07-14"
    assert frame.loc[0, "financing_balance_yuan"] == 1_452_014_185_914
    assert frame.loc[0, "securities_lending_balance_yuan"] == 13_885_066_040


def test_hkex_summary_converts_millions_and_keeps_northbound_net_empty(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = [
        {
            "id": 0,
            "date": "2026-07-15",
            "market": "SSE Northbound",
            "tradingDay": 1,
            "content": [
                {
                    "style": 1,
                    "table": {
                        "schema": [["Total Turnover", "Total Trade Count", "DQB", "ETF Turnover"]],
                        "tr": [
                            {"td": [["166,377.50"]]},
                            {"td": [["7,233,623"]]},
                            {"td": [["999,999,999"]]},
                            {"td": [["3,790.99"]]},
                        ],
                    },
                },
                {
                    "style": 2,
                    "table": {
                        "schema": [["Rank", "Stock Code", "Stock Name", "Total Turnover"]],
                        "tr": [
                            {"td": [["1", "688256", "寒武紀\u3000\u3000", "1,234"]]}
                        ],
                    },
                },
            ],
        },
        {
            "id": 1,
            "date": "2026-07-15",
            "market": "SSE Southbound",
            "tradingDay": 1,
            "content": [
                {
                    "style": 1,
                    "table": {
                        "schema": [["Total Turnover", "Buy Turnover", "Sell Turnover", "Total Trade Count", "Buy Trade Count", "Sell Trade Count", "ETF Turnover"]],
                        "tr": [
                            {"td": [["78,394.57"]]},
                            {"td": [["41,446.00"]]},
                            {"td": [["36,948.57"]]},
                            {"td": [["1,382,619"]]},
                            {"td": [["736,214"]]},
                            {"td": [["646,405"]]},
                            {"td": [["2,205.59"]]},
                        ],
                    },
                }
            ],
        },
    ]
    wrapper = "\ufefftabData = " + json.dumps(payload) + ";"
    request: dict[str, object] = {}

    def fake_get(*args: object, **kwargs: object) -> FakeResponse:
        request["url"] = args[1]
        request["headers"] = kwargs.get("headers")
        return FakeResponse(wrapper, "application/javascript")

    monkeypatch.setattr(official, "get", fake_get)

    summary, top, _ = adapters.fetch_hkex_connect("2026-07-15")

    north = summary[summary["direction"] == "northbound"].iloc[0]
    south = summary[summary["direction"] == "southbound"].iloc[0]
    assert north["total_turnover"] == pytest.approx(166_377_500_000)
    assert pd.isna(north["net_buy"])
    assert pd.isna(north["daily_quota_balance"])
    assert south["net_buy"] == pytest.approx(4_497_430_000)
    assert len(top) == 1
    assert top.loc[0, "instrument_code"] == "688256"
    assert top.loc[0, "instrument_name"] == "寒武紀"
    assert "/chi/csm/DailyStat/" in str(request["url"])
    assert str(request["url"]).endswith("20260715c.js")
    assert "sc_lang=zh-CN" in str(request["headers"])


def test_hkex_archives_official_404_for_expired_history(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    missing = MissingResponse()
    error = official.requests.HTTPError("404")
    error.response = missing  # type: ignore[assignment]

    def missing_get(*args: object, **kwargs: object) -> FakeResponse:
        raise error

    monkeypatch.setattr(official, "get", missing_get)

    summary, top, artifact = adapters.fetch_hkex_connect("2024-08-01")

    assert summary.empty
    assert top.empty
    assert artifact.status_code == 404
    assert artifact.normalized_path is not None
    assert "官方文件返回 404" in artifact.warnings[0]


def test_hkex_accepts_javascript_only_string_escapes(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    wrapper = r'''tabData = [{
      "date":"2026-02-10",
      "market":"SSE Southbound",
      "tradingDay":1,
      "content":[{
        "style":2,
        "table":{
          "schema":[["Rank","Stock Code","Stock Name","Total Turnover"]],
          "tr":[{"td":[["1","00001","L\'Occitane\x20Intl","1,234"]]}]
        }
      }]
    }];'''

    monkeypatch.setattr(
        official,
        "get",
        lambda *args, **kwargs: FakeResponse(wrapper, "application/javascript"),
    )

    summary, top, artifact = adapters.fetch_hkex_connect("2026-02-10")

    assert summary.empty
    assert top.loc[0, "instrument_name"] == "L'Occitane Intl"
    assert artifact.status_code == 200


def test_hkex_missing_name_uses_small_override_and_keeps_source_name(
    monkeypatch: pytest.MonkeyPatch, adapters: OfficialMarketAdapters
) -> None:
    payload = [
        {
            "date": "2026-08-06",
            "market": "SSE Southbound",
            "tradingDay": 1,
            "content": [
                {
                    "style": 2,
                    "table": {
                        "schema": [["Rank", "Stock Code", "Stock Name", "Total Turnover"]],
                        "tr": [
                            {"td": [["2", "00100", "\u3000\u3000", "1,234"]]},
                            {"td": [["3", "09999", "\u3000", "100"]]},
                        ],
                    },
                }
            ],
        }
    ]
    wrapper = "tabData = " + json.dumps(payload, ensure_ascii=False) + ";"
    monkeypatch.setattr(
        official,
        "get",
        lambda *args, **kwargs: FakeResponse(wrapper, "application/javascript"),
    )

    _, top, _ = adapters.fetch_hkex_connect("2026-08-06")

    assert top.loc[0, "instrument_code"] == "00100"
    assert top.loc[0, "instrument_name"] == "MINIMAX-W"
    assert pd.isna(top.loc[0, "instrument_name_raw"])
    assert top.loc[0, "instrument_name_source"] == "override"
    assert pd.isna(top.loc[1, "instrument_name"])
    assert top.loc[1, "instrument_name_source"] == "unresolved"
