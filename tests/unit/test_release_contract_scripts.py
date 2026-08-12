from __future__ import annotations

import pytest

from scripts import build_stock_pressure_state as pressure_state


class _Response:
    def __init__(self, payload, *, status_code: int = 200, release_id: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"x-flow-release-id": release_id} if release_id else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


def _item(code: str, as_of: str) -> dict:
    return {
        "listingMarket": "101",
        "stockCode": code,
        "stockName": f"股票{code}",
        "asOf": as_of,
        "netShares": 1,
        "inflowShares": 1,
        "outflowShares": 0,
        "etfCount": 1,
    }


def test_previous_release_uses_exact_release_id_for_every_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    release_id = "release:2026-08-07:0123456789abcdef"
    calls: list[tuple[str, dict]] = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        if url.endswith("previous-candidates"):
            return _Response({"candidates": [{"releaseId": release_id, "asOf": "2026-08-07"}]})
        name = kwargs["params"]["name"]
        if name == "pressure":
            payload = {"asOf": "2026-08-07", "data": {"pressure": {"schemaVersion": 2}}}
        elif name == "stock_search":
            payload = {"asOf": "2026-08-07", "items": [{"code": "600000"}]}
        else:
            payload = {"asOf": "2026-08-07", "items": [_item("600000", "2026-08-07")]}
        return _Response(payload, release_id=release_id)

    monkeypatch.setattr(pressure_state.requests, "get", fake_get)
    items, continuity = pressure_state._fetch_previous_items(
        "https://flow.example",
        1,
        headers={"Authorization": "Bearer test"},
        before_date="2026-08-10",
    )
    assert items[0]["stockCode"] == "600000"
    assert continuity["previousReleaseId"] == release_id
    artifact_calls = [params["releaseId"] for url, params in calls if url.endswith("artifact")]
    assert artifact_calls == [release_id] * 10


def test_previous_candidates_empty_is_explicit_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pressure_state.requests,
        "get",
        lambda *_args, **_kwargs: _Response({"status": "not_found", "candidates": []}),
    )
    items, continuity = pressure_state._fetch_previous_items("https://flow.example", 1)
    assert items == []
    assert continuity["historyStatus"] == "bootstrapped"
