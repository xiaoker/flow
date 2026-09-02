from __future__ import annotations

import gzip
import hashlib

import pytest

from scripts.fetch_stock_flow_history_inputs import _fetch


class FakeResponse:
    def __init__(self, content: bytes, *, headers: dict[str, str], status_code: int = 200) -> None:
        self.content = content
        self.headers = headers
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
        return self.response


def _csv() -> bytes:
    return b"trade_date,instrument_code,shares\n2026-07-31,510300,100\n"


def test_fetch_reconstructs_missing_legacy_sha_from_immutable_body() -> None:
    payload = _csv()
    frame, status = _fetch(
        FakeSession(
            FakeResponse(
                payload,
                headers={
                    "content-type": "text/csv; charset=utf-8",
                    "x-flow-normalized-key": "market-data/normalized/szse/etf_scale/2026-07-31/revision/data.csv",
                },
            )
        ),
        url="https://example.test/history-artifact",
        params={"date": "2026-07-31"},
        headers={},
        attempts=1,
    )

    assert frame is not None and len(frame) == 1
    assert status["sha256"] == hashlib.sha256(payload).hexdigest()
    assert status["sha256Source"] == "computed-object-body"
    assert status["wireSha256"] == status["contentSha256"] == status["sha256"]


@pytest.mark.parametrize("declared_scope", ["object", "content"])
def test_fetch_verifies_both_supported_gzip_sha_scopes(declared_scope: str) -> None:
    content = _csv()
    wire = gzip.compress(content, mtime=0)
    expected = hashlib.sha256(wire if declared_scope == "object" else content).hexdigest()
    frame, status = _fetch(
        FakeSession(
            FakeResponse(
                wire,
                headers={
                    "content-type": "application/gzip",
                    "x-flow-normalized-key": "market-data/normalized/sse/etf_pcf/2026-08-31/revision/data.csv.gz",
                    "x-flow-normalized-sha256": expected,
                },
            )
        ),
        url="https://example.test/history-artifact",
        params={"date": "2026-08-31"},
        headers={},
        attempts=1,
    )

    assert frame is not None and len(frame) == 1
    assert status["sha256"] == expected
    assert status["sha256Source"] == f"header-verified-{declared_scope}"


def test_fetch_rejects_declared_sha_that_matches_neither_object_nor_content() -> None:
    with pytest.raises(RuntimeError, match="archived artifact sha256 mismatch"):
        _fetch(
            FakeSession(
                FakeResponse(
                    _csv(),
                    headers={
                        "content-type": "text/csv; charset=utf-8",
                        "x-flow-normalized-key": "market-data/normalized/sse/etf_scale/2026-08-31/revision/data.csv",
                        "x-flow-normalized-sha256": "0" * 64,
                    },
                )
            ),
            url="https://example.test/history-artifact",
            params={"date": "2026-08-31"},
            headers={},
            attempts=1,
        )
