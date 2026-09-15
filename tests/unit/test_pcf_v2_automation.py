from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.audit_pcf_v2_legacy_history import audit_legacy_history
from scripts.build_pcf_v2_plan import build_plan
from scripts.finalize_sse_pcf_v2 import classify_capture_items, finalize_plan
from scripts.summarize_pcf_v2_history import summarize_jobs
from trading_desk.pcf_xml import hash_normalized_pcf_rows, parse_sse_etf_pcf_xml


FIXTURE = Path(__file__).parents[1] / "fixtures" / "pcf" / "same-day.xml"


class Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        payload: dict | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload or {}).encode()
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = self.content.decode("utf-8", errors="replace")
        self.ok = 200 <= status_code < 300

    def json(self) -> dict:
        if self._payload is not None:
            return self._payload
        return json.loads(self.content.decode())


class FinalizeSession:
    def __init__(self, plan: dict, checkpoint_items: list[dict] | None = None) -> None:
        self.plan = plan
        self.job_id = f"pcf:{plan['targetDate']}:{plan['planSha256'][:16]}"
        self.put_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.checkpoint_items = checkpoint_items or [{"etfCode": "510300", "state": "CAPTURED"}]

    def post(self, url: str, **kwargs: object) -> Response:
        self.post_calls.append({"url": url, **kwargs})
        if url.endswith("/finalize"):
            return Response(payload={"status": "READY", "jobId": self.job_id})
        return Response(payload={"jobId": self.job_id, "idempotent": True})

    def get(self, url: str, **kwargs: object) -> Response:
        if url.endswith("/pcf-capture"):
            return Response(payload={"items": self.checkpoint_items})
        raw = FIXTURE.read_bytes()
        wire = gzip.compress(raw, compresslevel=6, mtime=0)
        rows, _, warnings = parse_sse_etf_pcf_xml(
            raw,
            requested_code="510300",
            target_date="2026-08-21",
            previous_date="2026-08-20",
        )
        assert warnings == []
        return Response(
            content=wire,
            headers={
                "Content-Type": "application/gzip",
                "X-Flow-Object-Sha256": hashlib.sha256(wire).hexdigest(),
                "X-Flow-Raw-Sha256": hashlib.sha256(raw).hexdigest(),
                "X-Flow-Source-Trading-Day": "2026-08-21",
                "X-Flow-Source-Previous-Day": "2026-08-20",
                "X-Flow-Observed-At": "2026-08-21T10:30:00Z",
                "X-Flow-Normalized-Sha256": hash_normalized_pcf_rows(rows),
            },
        )

    def put(self, url: str, **kwargs: object) -> Response:
        self.put_calls.append({"url": url, **kwargs})
        return Response(payload={"idempotent": False, "size": len(kwargs.get("data", b""))})


def test_plan_accepts_only_sub_share_float_serialization_noise(tmp_path: Path) -> None:
    path = tmp_path / "shares.csv"
    pd.DataFrame(
        [
            {"trade_date": "2026-08-20", "market": "SSE", "instrument_code": "510300", "instrument_name": "300ETF", "shares": "7633566800"},
            {"trade_date": "2026-08-21", "market": "SSE", "instrument_code": "510300", "instrument_name": "300ETF", "shares": "7633566800.000001"},
            {"trade_date": "2026-08-20", "market": "SZSE", "instrument_code": "159919", "instrument_name": "300ETF", "shares": "10"},
            {"trade_date": "2026-08-21", "market": "SZSE", "instrument_code": "159919", "instrument_name": "300ETF", "shares": "20"},
        ]
    ).to_csv(path, index=False)
    plan = build_plan(path, trade_date="2026-08-21", previous_date="2026-08-20")
    assert plan["status"] == "READY_NO_CHANGE"
    assert plan["counts"]["expected"] == 1
    assert plan["items"][0]["market"] == "SSE"


def test_finalize_builds_exact_canonical_and_never_publishes_product_read_model() -> None:
    plan = {
        "targetDate": "2026-08-21",
        "previousDate": "2026-08-20",
        "calculationVersion": "pcf-capture-v2",
        "hardLimit": 2000,
        "status": "PLANNED",
        "planSha256": "a" * 64,
        "items": [
            {
                "market": "SSE",
                "etfCode": "510300",
                "etfName": "300ETF",
                "currentShares": 2,
                "previousShares": 1,
                "shareDelta": 1,
                "baselineStatus": "COMPARABLE_CHANGED",
                "required": True,
                "state": "PENDING",
            }
        ],
        "counts": {"expected": 1, "required": 1, "unchanged": 0, "newBaselineMissing": 0, "missingCurrent": 0, "invalidShare": 0},
    }
    session = FinalizeSession(plan)
    result = finalize_plan(plan, base_url="https://flow.example", api_key="secret", session=session)
    assert result["status"] == "READY"
    assert result["coverage"] == {"required": 1, "captured": 1, "valid": 1, "failed": 0}
    assert result["productionPublishCalls"] == 0
    assert len(session.put_calls) == 5
    finalize_call = next(call for call in session.post_calls if call["url"].endswith("/finalize"))
    assert finalize_call["json"]["validatedItems"][0]["tradingDay"] == "2026-08-21"
    assert len(finalize_call["json"]["validatedItems"][0]["normalizedSha256"]) == 64


def test_capture_classification_does_not_call_deferred_items_failures() -> None:
    result = classify_capture_items(
        {"510300", "510500", "510700", "510900"},
        [
            {"etfCode": "510300", "state": "CAPTURED"},
            {"etfCode": "510500", "state": "PENDING"},
            {"etfCode": "510700", "state": "BLOCKED", "failureReason": "http_403", "attempts": 1},
        ],
    )

    assert result["capturedCodes"] == ["510300"]
    assert result["pendingCodes"] == ["510500"]
    assert result["blockedCodes"] == ["510700"]
    assert result["unknownCodes"] == ["510900"]
    assert result["unavailableCodes"] == ["510500", "510700", "510900"]
    assert result["blockedItems"] == [{"code": "510700", "reason": "http_403", "attempts": 1}]


def test_finalize_reports_pending_separately_from_blocked_items() -> None:
    plan = {
        "targetDate": "2026-08-21",
        "previousDate": "2026-08-20",
        "calculationVersion": "pcf-capture-v2",
        "hardLimit": 2000,
        "status": "PLANNED",
        "planSha256": "b" * 64,
        "items": [
            {"etfCode": "510300", "required": True},
            {"etfCode": "510500", "required": True},
            {"etfCode": "510700", "required": True},
        ],
    }
    session = FinalizeSession(
        plan,
        checkpoint_items=[
            {"etfCode": "510300", "state": "CAPTURED"},
            {"etfCode": "510500", "state": "PENDING"},
            {"etfCode": "510700", "state": "BLOCKED", "failureReason": "http_403", "attempts": 1},
        ],
    )

    result = finalize_plan(plan, base_url="https://flow.example", api_key="secret", session=session)

    assert result["status"] == "PARTIAL"
    assert result["coverage"] == {
        "required": 3,
        "captured": 1,
        "valid": 0,
        "failed": 1,
        "pending": 1,
        "unknown": 0,
    }
    assert result["pendingCodes"] == ["510500"]
    assert result["blockedCodes"] == ["510700"]
    assert result["unknownCodes"] == []
    assert result["blockedItems"][0]["reason"] == "http_403"
    assert result["unavailableCodes"] == ["510500", "510700"]
    assert session.put_calls == []


def test_legacy_next_day_baskets_are_explicitly_ineligible_for_v2() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": "2026-08-21",
                "pcf_date": "2026-08-24",
                "pcf_previous_date": "2026-08-21",
                "date_basis": "sse_pcf_next_day_previous_trading_day",
            }
        ]
    )
    report = audit_legacy_history(frame, ["2026-08-21", "2026-08-24"])
    assert report["eligibleDates"] == []
    assert report["dates"][0]["status"] == "LEGACY_ESTIMATE_ONLY"
    assert report["dates"][1]["status"] == "MISSING_LEGACY_EVIDENCE"
    assert report["policy"]["copyCurrentNode"] is False


def test_five_day_gate_requires_real_ready_jobs_and_exact_previous_dates() -> None:
    dates = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"]
    observations = []
    for index, trade_date in enumerate(dates):
        observations.append(
            {
                "tradeDate": trade_date,
                "status": "READY",
                "job": {
                    "previous_date": dates[index - 1] if index else "2026-08-20",
                    "required_count": 2,
                    "captured_count": 2,
                    "valid_count": 2,
                    "failed_count": 0,
                    "coverage_ppm": 1_000_000,
                    "canonical_manifest_key": f"manifest/{trade_date}",
                },
            }
        )
    passed = summarize_jobs(observations, dates, required_days=5)
    assert passed["status"] == "PASS"
    observations[-1]["job"]["previous_date"] = "2026-08-25"
    failed = summarize_jobs(observations, dates, required_days=5)
    assert failed["status"] == "FAIL"
    assert failed["failedDates"] == ["2026-08-27"]
