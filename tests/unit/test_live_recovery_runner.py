from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_live_recovery as runner
from scripts.run_live_recovery import (
    BudgetedRateLimitedSession,
    LiveRecoveryError,
    _post_child_request_reserve,
    _assert_szse_current_snapshot_is_adjacent,
    _current_szse_etf_matches_target,
    _szse_history_request_budget,
    _fetch_market_history,
    run_live_recovery,
)
from trading_desk.recovery_execution import build_recovery_plan


def _plan(
    dates: list[str],
    *,
    window_date: str = "2026-08-28",
    budget_manifest: dict[str, object] | None = None,
) -> dict:
    return build_recovery_plan(
        campaign_id="recovery-20260904",
        recovery_id="recovery-20260904-live",
        run_key=f"daily-market-data:{window_date}:reconcile:v1",
        data_dates=dates,
        required_stages=["dashboard", "publish"],
        artifact_root=".recovery/recovery-20260904-live",
        state_dir=".recovery/recovery-20260904-live/state",
        max_dates=3,
        max_requests=100,
        deadline="2026-12-31T00:00:00.000Z",
        budget_manifest=budget_manifest
        or {"source": "test", "measured": True, "szseHistoryMaxRequests": 20},
    )


def test_live_runner_fails_closed_instead_of_silently_using_last_date(tmp_path: Path) -> None:
    with pytest.raises(LiveRecoveryError, match="live_recovery_multi_date_requires_date_major_dispatch") as caught:
        run_live_recovery(
            plan=_plan(["2026-08-27", "2026-08-28"]),
            expected_plan_hash=None,
            attempt=1,
            producer_build_sha="a" * 64,
            workspace_root=tmp_path,
            base_url="https://flow.example",
        )
    assert caught.value.status == "BLOCKED_QUALITY"


class _Transport:
    def __init__(self) -> None:
        self.headers = {}
        self.request_timeout = (1.0, 2.0)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs: object):
        self.calls.append((method, url))
        return object()


class _StatusTransport(_Transport):
    def __init__(self, status_code: int) -> None:
        super().__init__()
        self.status_code = status_code

    def request(self, method: str, url: str, **_kwargs: object):
        self.calls.append((method, url))
        return type("Response", (), {"status_code": self.status_code})()


def test_budgeted_session_counts_get_and_post_and_enforces_budget() -> None:
    transport = _Transport()
    session = BudgetedRateLimitedSession(transport, maximum=2, min_interval_seconds=0)
    session.get("https://source.example/a")
    session.post("https://source.example/b")
    assert session.request_count == 2
    assert transport.calls == [("GET", "https://source.example/a"), ("POST", "https://source.example/b")]
    with pytest.raises(LiveRecoveryError, match="live_recovery_request_budget_exceeded"):
        session.get("https://source.example/c")


def test_budgeted_session_paces_same_host_without_delaying_other_hosts(monkeypatch) -> None:
    clock = [100.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", sleep)
    session = BudgetedRateLimitedSession(_Transport(), maximum=4)
    session.get("https://source.example/first")
    session.get("https://other.example/first")
    session.post("https://SOURCE.EXAMPLE/second")
    clock[0] += 0.5
    session.get("https://source.example/third")
    assert sleeps == [2.0, 1.5]
    assert session.last_by_host == {"source.example": 104.0, "other.example": 100.0}
    assert session.request_count == 4


class _RecoveryTransport(_Transport):
    """Offline boundary fixture, not evidence of a live recovery."""

    def __init__(self, archive_status="archived"):
        super().__init__()
        self.archive_status = archive_status
        self.checkpoint = False
        self.events = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        target = "2026-08-28"
        if url.endswith("/etf-state"):
            # The target already exists in ETF state but is absent from the
            # independent market-history checkpoint. This was a dead end.
            self.events.append("state")
            value = {"version": 1, "dates": ["2026-08-27", target], "instruments": []}
        elif url.endswith("/history-import"):
            payload = json.loads(kwargs["data"])
            if payload["history"]["finalize"]:
                self.events.append("finalize")
                self.checkpoint = self.archive_status == "archived"
                value = {"status": self.archive_status, "asOf": target}
            else:
                self.events.append("chunk")
                value = {"status": "chunk_archived"}
        elif url.endswith("/market-history"):
            self.events.append("history")
            value = {"rows": [{"trade_date": "2026-08-27"}]}
            if self.checkpoint:
                value["rows"].append({"trade_date": target})
        elif url.endswith("/releases/latest"):
            self.events.append("latest_readonly")
            value = {"releaseId": "release:2026-09-03:existing", "asOf": "2026-09-03"}
        else:
            raise AssertionError(f"Unexpected remote operation: {method} {url}")
        response = _JsonResponse(value)
        response.ok = True
        response.status_code = 200
        return response


@pytest.fixture
def offline_runner(monkeypatch):
    for key in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(key, "offline-fixture-only")
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runner, "discover_recent_trading_dates", lambda **_kwargs: ["2026-08-26", "2026-08-27", "2026-08-28"])

    def collect(**kwargs):
        manifest = {
            "run_id": "fixture",
            "artifacts": [{"releaseId": "fixture-source"}],
            "szse_etf_page_date": "2026-08-29",
            "szse_etf_trade_date": "2026-08-28",
        }
        quality = {"counts": {"fail": 0}}
        runner._write_json(kwargs["report_root"] / "fixture" / "manifest.json", manifest)
        runner._write_json(kwargs["report_root"] / "fixture" / "quality-summary.json", quality)
        return {"manifest": manifest, "quality": quality}

    def snapshot(**kwargs):
        runner._write_json(kwargs["stock_catalog_path"], {"available": False, "stocks": []})
        return {"asOf": "2026-08-28", "publication": {"status": "confirmed"}, "metrics": [], "dataStatus": []}

    def bundle(**kwargs):
        snapshot = runner._read_json(kwargs["snapshot_path"])
        return {"version": 1, "history": {"asOf": snapshot["asOf"], "snapshot": snapshot, "artifacts": [{"releaseId": "fixture-source"}]}}

    monkeypatch.setattr(runner, "collect_phase0", collect)
    monkeypatch.setattr(runner, "_current_szse_etf_matches_target", lambda **_kwargs: False)
    monkeypatch.setattr(
        runner,
        "_invoke_szse_etf_history",
        lambda **_kwargs: (None, 0),
    )
    monkeypatch.setattr(runner, "build_dashboard_snapshot", snapshot)
    monkeypatch.setattr(runner, "build_history_bundle_from_files", bundle)

    def execute(tmp_path, *, window_date="2026-08-28", archive_status="archived", current_szse=False):
        transport = _RecoveryTransport(archive_status)
        monkeypatch.setattr(runner, "build_session", lambda _settings: transport)
        monkeypatch.setattr(runner, "_current_szse_etf_matches_target", lambda **_kwargs: current_szse)
        if current_szse:
            monkeypatch.setattr(
                runner,
                "_invoke_szse_etf_history",
                lambda **_kwargs: (_ for _ in ()).throw(AssertionError("current page must not fetch per-fund history")),
            )
        plan = _plan(["2026-08-28"], window_date=window_date)
        result = runner.run_live_recovery(plan=plan, expected_plan_hash=plan["planHash"], attempt=1,
            producer_build_sha="a" * 64, workspace_root=tmp_path, base_url="https://flow.example")
        return result, transport

    return execute


def test_existing_etf_state_does_not_skip_missing_market_checkpoint(tmp_path, offline_runner):
    result, transport = offline_runner(tmp_path)
    assert transport.events == ["state", "chunk", "finalize", "history", "latest_readonly"]
    assert result["status"] == "READY"
    assert result["requestCount"] == 5
    assert result["targetDate"] == "2026-08-28"
    assert result["szseSourceMode"] == "history"
    assert len(result["historyPublications"]) == 1
    assert not any(method == "POST" and not url.endswith("/history-import") for method, url in transport.calls)


def test_adjacent_current_page_skips_per_fund_history(tmp_path, offline_runner):
    result, transport = offline_runner(tmp_path, current_szse=True)
    assert result["szseSourceMode"] == "current"
    assert result["szseHistoryRequestCount"] == 0
    assert result["targetDate"] == "2026-08-28"
    assert transport.events == ["state", "chunk", "finalize", "history", "latest_readonly"]


def test_runner_keeps_window_date_separate_from_data_date(tmp_path, offline_runner):
    result, _ = offline_runner(tmp_path, window_date="2026-09-01")
    assert result["runKey"] == "daily-market-data:2026-09-01:reconcile:v1"
    assert result["windowDate"] == "2026-09-01"
    assert result["dataDates"] == ["2026-08-28"]
    assert result["targetDate"] == "2026-08-28"


@pytest.mark.parametrize("archive_status", ["waiting_sources", "archived_pending"])
def test_pending_archive_is_not_a_complete_checkpoint(tmp_path, offline_runner, archive_status):
    with pytest.raises(LiveRecoveryError, match="live_recovery_history_checkpoint_incomplete") as caught:
        offline_runner(tmp_path, archive_status=archive_status)
    assert caught.value.status == "FAILED_RETRYABLE"
    root = tmp_path / ".recovery/recovery-20260904-live"
    assert not (root / "recovery-evidence.json").exists()
    assert not (root / "source-bundle/market-history.json").exists()


@pytest.mark.parametrize("response", [
    {"status": "archived", "asOf": "2026-08-27"},
    {"status": "archived"},
    {"asOf": "2026-08-28"},
    {"status": "archived_pending", "asOf": "2026-08-28", "reason": "derived_etf_quality_failed"},
])
def test_checkpoint_requires_success_and_exact_business_date(response):
    with pytest.raises(LiveRecoveryError, match="live_recovery_history_checkpoint_incomplete") as caught:
        runner._assert_history_checkpoint(response, "2026-08-28")
    assert caught.value.status == "BLOCKED_QUALITY"


@pytest.mark.parametrize("status_code", [403, 429])
def test_budgeted_session_fails_closed_on_source_rejection(status_code: int) -> None:
    transport = _StatusTransport(status_code)
    session = BudgetedRateLimitedSession(transport, maximum=2, min_interval_seconds=0)
    with pytest.raises(LiveRecoveryError, match="live_recovery_source_unavailable"):
        session.get("https://source.example/rejected")
    assert session.request_count == 1
    assert len(transport.calls) == 1


def test_budgeted_session_uses_bounded_official_curl_for_szse(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return runner.subprocess.CompletedProcess(command, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", run)
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=2, min_interval_seconds=0, szse_curl=True
    )
    response = session.get(
        runner.SZSE_ETF_METADATA_URL,
        params={"SHOWTYPE": "JSON", "tab1PAGESIZE": "1"},
        headers={"Referer": runner.SZSE_ETF_PAGE_URL, "User-Agent": "fixture"},
    )
    assert response.status_code == 200
    assert response.content == b"[]"
    assert session.request_count == 1
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:7] == ["curl", "--silent", "--show-error", "--fail", "--retry", "0", "--max-time"]
    assert "--resolve" not in command
    assert "--retry" in command and command[command.index("--retry") + 1] == "0"
    assert kwargs["timeout"] == 90


def test_budgeted_session_prefers_official_www_host_and_records_transport(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return runner.subprocess.CompletedProcess(command, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", run)
    params = {"SHOWTYPE": "JSON", "_flow_internal": "must-not-be-sent"}
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=2, min_interval_seconds=0, szse_curl=True
    )
    response = session.get(
        runner.SZSE_ETF_METADATA_URL,
        params=params,
        headers={"Referer": runner.SZSE_CURL_PAGE_URL, "User-Agent": "fixture"},
    )

    command, _kwargs = calls[0]
    assert command[-1] == "https://www.szse.cn/api/report/ShowReport/data"
    assert "_flow_internal" not in command
    assert params["_flow_transport"] == "curl_www_szse_cn"
    assert params["_flow_source_url"] == response.url
    assert response.headers["X-Flow-Source-Transport"] == "curl_www.szse.cn"
    assert session.szse_active_host == "www.szse.cn"
    assert session.request_count == 1


def test_budgeted_session_falls_back_to_fund_host_once(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            return runner.subprocess.CompletedProcess(command, 22, stdout=b"", stderr=b"")
        return runner.subprocess.CompletedProcess(command, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", run)
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=2, min_interval_seconds=0, szse_curl=True
    )
    response = session.get(runner.SZSE_ETF_METADATA_URL)

    assert [call[0][-1] for call in calls] == [
        "https://www.szse.cn/api/report/ShowReport/data",
        runner.SZSE_ETF_METADATA_URL,
    ]
    assert response.url == runner.SZSE_ETF_METADATA_URL
    assert session.szse_active_host == "fund.szse.cn"
    assert session.request_count == 2


def test_budgeted_session_rejects_an_invalid_szse_host(monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RECOVERY_SZSE_HOST", "evil.example")
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=2, min_interval_seconds=0, szse_curl=True
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_source_unavailable") as caught:
        session.get(runner.SZSE_ETF_METADATA_URL)
    assert caught.value.status == "BLOCKED_QUALITY"
    assert session.request_count == 0


def test_budgeted_session_accepts_an_explicit_valid_szse_resolver(monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return runner.subprocess.CompletedProcess(command, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setenv("FLOW_RECOVERY_SZSE_RESOLVE", "fund.szse.cn:443:203.0.113.10")
    monkeypatch.setattr(runner.subprocess, "run", run)
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=1, min_interval_seconds=0, szse_curl=True
    )
    session.get(runner.SZSE_ETF_METADATA_URL)
    command, _kwargs = calls[0]
    assert command[command.index("--resolve") + 1] == "fund.szse.cn:443:203.0.113.10"


def test_budgeted_session_rejects_an_invalid_szse_resolver(monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RECOVERY_SZSE_RESOLVE", "fund.szse.cn:443:not-an-ip")
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=1, min_interval_seconds=0, szse_curl=True
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_source_unavailable") as caught:
        session.get(runner.SZSE_ETF_METADATA_URL)
    assert caught.value.status == "BLOCKED_QUALITY"
    assert session.request_count == 0


def test_szse_curl_failure_is_retryable_without_echoing_response(monkeypatch) -> None:
    def run(_command, **_kwargs):
        return runner.subprocess.CompletedProcess([], 22, stdout=b"secret-body", stderr=b"secret-url")

    monkeypatch.setattr(runner.subprocess, "run", run)
    session = BudgetedRateLimitedSession(
        _Transport(), maximum=1, min_interval_seconds=0, szse_curl=True
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_source_unavailable") as caught:
        session.get(runner.SZSE_ETF_METADATA_URL)
    assert "secret" not in str(caught.value)
    assert session.request_count == 1


def test_szse_history_budget_must_be_explicit_and_leave_parent_capacity() -> None:
    scope = runner.validate_recovery_plan(_plan(["2026-08-28"]))
    assert _szse_history_request_budget(scope) == 20

    missing_scope = runner.validate_recovery_plan(
        _plan(["2026-08-28"], budget_manifest={"source": "test", "measured": True})
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_szse_history_budget_missing"):
        _szse_history_request_budget(missing_scope)

    too_large_scope = runner.validate_recovery_plan(
        _plan(
            ["2026-08-28"],
            budget_manifest={
                "source": "test",
                "measured": True,
                "szseHistoryMaxRequests": 100,
            },
        )
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_szse_history_budget_invalid"):
        _szse_history_request_budget(too_large_scope)


def test_historical_szse_snapshot_accepts_only_immediate_preceding_business_date(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "discover_recent_trading_dates",
        lambda **_kwargs: ["2026-08-27", "2026-08-28"],
    )
    _assert_szse_current_snapshot_is_adjacent(
        manifest={"szse_etf_page_date": "2026-08-31"},
        target="2026-08-28",
        session=object(),
    )


def test_historical_szse_snapshot_rejects_a_later_moving_workbook(monkeypatch) -> None:
    monkeypatch.setattr(
        runner,
        "discover_recent_trading_dates",
        lambda **_kwargs: ["2026-09-09", "2026-09-10"],
    )
    with pytest.raises(
        LiveRecoveryError,
        match="live_recovery_historical_szse_source_required",
    ) as caught:
        _assert_szse_current_snapshot_is_adjacent(
            manifest={"szse_etf_page_date": "2026-09-11"},
            target="2026-08-28",
            session=object(),
        )
    assert caught.value.status == "BLOCKED_QUALITY"


def test_historical_szse_snapshot_rejects_missing_page_date() -> None:
    with pytest.raises(
        LiveRecoveryError,
        match="live_recovery_historical_szse_source_required",
    ):
        _assert_szse_current_snapshot_is_adjacent(
            manifest={},
            target="2026-08-28",
            session=object(),
        )


class _SzseMetadataTransport(_Transport):
    def __init__(self, page_date: str, scale_date: str) -> None:
        super().__init__()
        self.page_date = page_date
        self.scale_date = scale_date

    def request(self, method: str, url: str, **_kwargs: object):
        self.calls.append((method, url))
        return _JsonResponse([{"metadata": {
            "subname": self.page_date,
            "cols": {"dqgm": f"当前规模（{self.scale_date}）"},
        }}])


def test_current_szse_probe_uses_official_adjacent_page(monkeypatch) -> None:
    monkeypatch.setattr(runner, "discover_recent_trading_dates", lambda **_kwargs: ["2026-09-11", "2026-09-14"])
    transport = _SzseMetadataTransport("2026-09-15", "2026-09-14")
    session = BudgetedRateLimitedSession(transport, maximum=4, min_interval_seconds=0)
    assert _current_szse_etf_matches_target(
        session=session,
        target="2026-09-14",
        context_dates=["2026-09-10", "2026-09-11", "2026-09-14"],
    )
    assert session.request_count == 1
    assert len(transport.calls) == 1


def test_current_szse_probe_sends_later_page_to_exact_history(monkeypatch) -> None:
    monkeypatch.setattr(runner, "discover_recent_trading_dates", lambda **_kwargs: ["2026-09-14", "2026-09-15"])
    session = BudgetedRateLimitedSession(
        _SzseMetadataTransport("2026-09-16", "2026-09-15"),
        maximum=4,
        min_interval_seconds=0,
    )
    assert not _current_szse_etf_matches_target(
        session=session,
        target="2026-09-14",
        context_dates=["2026-09-10", "2026-09-11", "2026-09-14"],
    )


def test_current_szse_probe_rejects_wrong_scale_label(monkeypatch) -> None:
    monkeypatch.setattr(runner, "discover_recent_trading_dates", lambda **_kwargs: ["2026-09-11", "2026-09-14"])
    session = BudgetedRateLimitedSession(
        _SzseMetadataTransport("2026-09-15", "2026-09-10"),
        maximum=4,
        min_interval_seconds=0,
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_szse_current_date_mismatch"):
        _current_szse_etf_matches_target(
            session=session,
            target="2026-09-14",
            context_dates=["2026-09-10", "2026-09-11", "2026-09-14"],
        )


def test_post_child_reserve_covers_history_reads_and_retry_attempts() -> None:
    assert _post_child_request_reserve({"artifacts": [{"releaseId": "a"}, {"releaseId": "b"}]}) == 11
    with pytest.raises(LiveRecoveryError, match="live_recovery_history_artifacts_missing"):
        _post_child_request_reserve({"artifacts": []})


class _JsonResponse:
    def __init__(self, value: dict) -> None:
        self._value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._value


class _JsonSession:
    def __init__(self, values: list[dict]) -> None:
        self.values = iter(values)

    def get(self, *_args: object, **_kwargs: object) -> _JsonResponse:
        return _JsonResponse(next(self.values))


def test_market_history_must_contain_target_date(tmp_path: Path) -> None:
    session = _JsonSession(
        [
            {"rows": [{"trade_date": "2026-08-27"}]},
            {"releaseId": "release-before"},
        ]
    )
    with pytest.raises(LiveRecoveryError, match="live_recovery_market_history_target_missing"):
        _fetch_market_history(
            base_url="https://flow.example",
            target="2026-08-28",
            output_dir=tmp_path,
            headers={"CF-Access-Client-Id": "id"},
            session=session,
        )
