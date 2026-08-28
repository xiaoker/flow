from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import requests

from scripts.build_pcf_v2_plan import build_current_official_plan
from scripts.capture_sse_pcf_v2 import capture_plan, fetch_xml, upload_xml
from trading_desk.pcf_xml import hash_normalized_pcf_rows, parse_sse_etf_pcf_xml


FIXTURE = Path(__file__).parents[1] / "fixtures" / "pcf" / "same-day.xml"


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b'{"state":"CAPTURED"}', content_type: str = "application/json") -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {
            "Content-Type": content_type,
            "Content-Disposition": 'attachment; filename="515590.xml"',
        }

    def json(self) -> dict[str, object]:
        return json.loads(self.content.decode("utf-8"))


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.get_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        self.get_calls.append(kwargs)
        return next(self.responses)

    def put(self, *args: object, **kwargs: object) -> FakeResponse:
        self.put_calls.append(kwargs)
        return next(self.responses)


class CaptureSession:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, object]] = []
        self.put_calls: list[dict[str, object]] = []
        self.post_calls: list[dict[str, object]] = []

    def post(self, *args: object, **kwargs: object) -> FakeResponse:
        self.post_calls.append(kwargs)
        if isinstance(kwargs.get("json"), dict) and kwargs["json"].get("action") == "acquire":
            return FakeResponse(200, content=b'{"acquired":true}')
        if isinstance(kwargs.get("json"), dict) and kwargs["json"].get("action") == "release":
            return FakeResponse(200, content=b'{"released":true}')
        return FakeResponse(200, content=b'{"jobId":"pcf:2026-08-21:0123456789abcdef"}')

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        self.get_calls.append(kwargs)
        if kwargs.get("params") == {"jobId": "pcf:2026-08-21:0123456789abcdef"}:
            return FakeResponse(200, content=b'{"items":[{"etfCode":"510500","state":"CAPTURED"}]}' )
        return FakeResponse(200, content=FIXTURE.read_bytes(), content_type="application/xml")

    def put(self, *args: object, **kwargs: object) -> FakeResponse:
        self.put_calls.append(kwargs)
        return FakeResponse(200)


def test_fetch_xml_does_not_retry_403() -> None:
    session = FakeSession([FakeResponse(403)])
    calls: list[float] = []
    try:
        fetch_xml(session, code="510300", target_date="2026-08-21", sleep_fn=calls.append)
    except Exception as exc:
        assert type(exc).__name__ == "RateLimited"
    else:
        raise AssertionError("expected rate limit")
    assert len(session.get_calls) == 1
    assert calls == []


def test_upload_xml_stream_contract_uses_content_hash_headers() -> None:
    raw = FIXTURE.read_bytes()
    rows, _, warnings = parse_sse_etf_pcf_xml(
        raw,
        requested_code="510300",
        target_date="2026-08-21",
        previous_date="2026-08-20",
    )
    assert warnings == []
    normalized_sha256 = hash_normalized_pcf_rows(rows)
    session = FakeSession([FakeResponse(200)])
    result = upload_xml(
        session,
        base_url="https://flow.example",
        api_key="secret",
        job_id="pcf:2026-08-21:0123456789abcdef",
        code="510300",
        target_date="2026-08-21",
        raw_xml=raw,
        component_count=len(rows),
        source_trading_day="2026-08-21",
        source_previous_day="2026-08-20",
        normalized_sha256=normalized_sha256,
    )

    assert result["state"] == "CAPTURED"
    assert len(session.put_calls) == 1
    request = session.put_calls[0]
    body = request["data"]
    headers = request["headers"]
    assert isinstance(body, bytes)
    assert headers["X-Flow-Raw-Sha256"] == hashlib.sha256(raw).hexdigest()
    assert headers["X-Flow-Object-Sha256"] == hashlib.sha256(body).hexdigest()
    assert headers["X-Flow-Capture-Outcome"] == "EXACT"
    assert headers["X-Flow-Normalized-Sha256"] == normalized_sha256
    assert str(headers["X-Flow-Object-Key"]).endswith(f"/{headers['X-Flow-Object-Sha256']}.xml.gz")


def test_upload_xml_escapes_unicode_diagnostics_for_http_headers() -> None:
    session = FakeSession([FakeResponse(200)])
    upload_xml(
        session,
        base_url="https://flow.example",
        api_key="secret",
        job_id="pcf:2026-08-21:0123456789abcdef",
        code="510300",
        target_date="2026-08-21",
        raw_xml=FIXTURE.read_bytes(),
        outcome="DATE_MISMATCH",
        failure_reason="TradingDay=2026-08-20 不等于目标日 2026-08-21",
    )

    reason = str(session.put_calls[0]["headers"]["X-Flow-Failure-Reason"])
    reason.encode("latin-1")
    assert "\\u4e0d" in reason
    assert "不" not in reason
    assert len(reason) <= 120


def test_current_plan_uses_live_pcf_dates_and_matching_official_share_snapshots(tmp_path: Path) -> None:
    shares_path = tmp_path / "known-shares.csv"
    pd.DataFrame(
        [
            {
                "trade_date": "2026-08-20",
                "market": "SSE",
                "instrument_code": "510300",
                "instrument_name": "300ETF",
                "shares": "10000",
            }
        ]
    ).to_csv(shares_path, index=False)
    current = {
        "result": [
            {"STAT_DATE": "2026-08-21", "SEC_CODE": "510300", "SEC_NAME": "300ETF", "TOT_VOL": "2"}
        ]
    }
    previous = {
        "result": [
            {"STAT_DATE": "2026-08-20", "SEC_CODE": "510300", "SEC_NAME": "300ETF", "TOT_VOL": "1"}
        ]
    }
    session = FakeSession(
        [
            FakeResponse(200, FIXTURE.read_bytes(), "application/xml"),
            FakeResponse(200, json.dumps(current, ensure_ascii=False).encode()),
            FakeResponse(200, json.dumps(previous, ensure_ascii=False).encode()),
        ]
    )
    evidence_dir = tmp_path / "evidence"

    plan = build_current_official_plan(
        shares_path,
        evidence_dir=evidence_dir,
        session=session,
    )

    assert plan["targetDate"] == "2026-08-21"
    assert plan["previousDate"] == "2026-08-20"
    assert plan["counts"]["required"] == 1
    assert plan["items"][0]["shareDelta"] == 10_000
    assert (evidence_dir / "2026-08-21-date-evidence.json").is_file()
    assert (evidence_dir / "2026-08-21-pcf-probe-510300.xml").read_bytes() == FIXTURE.read_bytes()


def test_capture_plan_skips_items_already_checkpointed_by_control_plane() -> None:
    session = CaptureSession()
    plan = {
        "status": "PLANNED",
        "targetDate": "2026-08-21",
        "previousDate": "2026-08-20",
        "items": [
            {"etfCode": "510300", "required": True},
            {"etfCode": "510500", "required": True},
        ],
    }
    result = capture_plan(plan, base_url="https://flow.example", api_key="secret", session=session, no_sleep=True)
    assert result["skipped"] == 1
    assert result["captured"] == 1
    assert len(session.put_calls) == 1
    assert len(session.get_calls) == 2
