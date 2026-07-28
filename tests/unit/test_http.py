from __future__ import annotations

from typing import Any

import pytest
import requests

from trading_desk.http import get


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} response")


class FakeSession:
    request_timeout = (1.0, 2.0)

    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return FakeResponse(self.statuses.pop(0))


def _set_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FLOW_SOURCE_PROXY_URL",
        "https://flow.xiaoker.com/api/v1/admin/source-proxy",
    )
    monkeypatch.setenv("FLOW_INGEST_API_KEY", "ingest-key")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "access-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "access-secret")


def test_records_direct_sse_transport() -> None:
    session = FakeSession([200])
    params = {"STAT_DATE": "2026-07-27"}

    response = get(
        session, "https://query.sse.com.cn/commonQuery.do", params=params
    )

    assert response.status_code == 200
    assert params["_flow_transport"] == "direct"
    assert len(session.calls) == 1


def test_falls_back_to_authenticated_cloudflare_sse_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_proxy_environment(monkeypatch)
    session = FakeSession([403, 200])
    params = {
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": "2026-07-27",
    }

    response = get(
        session,
        "https://query.sse.com.cn/commonQuery.do",
        params=params,
        headers={"Referer": "https://www.sse.com.cn/"},
    )

    assert response.status_code == 200
    assert params["_flow_transport"] == "cloudflare_sse_relay"
    proxy_url, proxy_call = session.calls[1]
    assert proxy_url == "https://flow.xiaoker.com/api/v1/admin/source-proxy"
    assert proxy_call["params"]["path"] == "/commonQuery.do"
    assert proxy_call["params"]["STAT_DATE"] == "2026-07-27"
    assert proxy_call["headers"] == {
        "Authorization": "Bearer ingest-key",
        "CF-Access-Client-Id": "access-id",
        "CF-Access-Client-Secret": "access-secret",
    }


def test_never_relays_non_sse_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_proxy_environment(monkeypatch)
    session = FakeSession([403])

    with pytest.raises(requests.HTTPError):
        get(session, "https://example.com/data")

    assert len(session.calls) == 1
