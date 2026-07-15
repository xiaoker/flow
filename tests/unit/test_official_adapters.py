from __future__ import annotations

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
                }
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
    wrapper = "tabData = " + json.dumps(payload) + ";"
    monkeypatch.setattr(
        official,
        "get",
        lambda *args, **kwargs: FakeResponse(wrapper, "application/javascript"),
    )

    summary, top, _ = adapters.fetch_hkex_connect("2026-07-15")

    north = summary[summary["direction"] == "northbound"].iloc[0]
    south = summary[summary["direction"] == "southbound"].iloc[0]
    assert north["total_turnover"] == pytest.approx(166_377_500_000)
    assert pd.isna(north["net_buy"])
    assert pd.isna(north["daily_quota_balance"])
    assert south["net_buy"] == pytest.approx(4_497_430_000)
    assert top.empty
