from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from trading_desk.pipeline_adapters import (
    BoundedOfficialSession,
    CoreCollectionStageAdapter,
    PipelineAdapterError,
)
from trading_desk.pipeline_application import canonical_sha256
from trading_desk.pipeline_runner import PipelineRunnerError, run_pipeline_request


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "web/package-lock.json"
PRODUCER_SHA = "a" * 64
IDENTITY = {
    "runKey": "daily-market-data:2026-09-30:reconcile:v1",
    "windowDate": "2026-09-30",
    "targetDate": "2026-09-29",
}
CORE_CONFIG = {
    "dates": ["2026-09-25", "2026-09-28", "2026-09-29"],
    "collectionWindow": "reconcile",
    "pcfMaxCodes": 16,
    "maxRequests": 80,
    "minIntervalSeconds": 2,
    "sourceFingerprint": "b" * 64,
}


def _payload(config: Mapping[str, Any] = CORE_CONFIG) -> dict[str, Any]:
    return {
        "identity": copy.deepcopy(IDENTITY),
        "mode": "ordinary",
        "lease": {
            "owner": "runner:fixture",
            "callbackOwner": "runner:fixture",
            "status": "ACTIVE",
            "expiresAt": "2099-01-01T00:00:00Z",
        },
        "authorization": {"kind": "existing-admin", "verified": True},
        "dependencyLockSha256": "",
        "stages": ["core_collection"],
        "stageInputs": {"core_collection": canonical_sha256(config)},
        "stageConfigs": {"core_collection": dict(config)},
    }


def _run(
    payload: dict[str, Any],
    tmp_path: Path,
    *,
    adapter: Any | None = None,
    verifier=lambda _context: True,
) -> dict[str, Any]:
    from trading_desk.pipeline_runner import dependency_lock_digest

    payload["dependencyLockSha256"] = dependency_lock_digest([LOCK], root=ROOT)["sha256"]
    return run_pipeline_request(
        payload,
        state_dir=tmp_path / "state",
        output_path=tmp_path / "result.json",
        producer_build_sha=PRODUCER_SHA,
        dependency_lock_paths=[LOCK],
        project_root=ROOT,
        now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        stage_adapters={"core_collection": adapter} if adapter is not None else None,
        authorization_verifier=verifier if adapter is not None else None,
    )


def _write_phase0_result(
    *,
    data_root: Path,
    report_root: Path,
    workspace_root: Path,
    quality_status: str = "pass",
) -> dict[str, Any]:
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "sample.csv").write_text("trade_date,code\n2026-09-29,510300\n", encoding="utf-8")
    report_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "phase0-adapter-fixture",
        "requested_dates": CORE_CONFIG["dates"],
        "artifacts": [{"dataset": "sse_etf", "requested_date": "2026-09-29"}],
    }
    quality = {"phase0_gate_status": quality_status, "phase0_gate_passed": quality_status == "pass"}
    (report_root / "latest-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    (report_root / "latest-quality-summary.json").write_text(
        json.dumps(quality, sort_keys=True), encoding="utf-8"
    )
    return {"manifest": manifest, "quality": quality}


def test_bounded_official_session_enforces_count_pacing_and_403_429_stop() -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class Session:
        headers: dict[str, str] = {}
        request_timeout = (1.0, 1.0)

        def __init__(self, status_code: int = 200) -> None:
            self.status_code = status_code
            self.calls: list[str] = []

        def get(self, url: str, **_kwargs: Any) -> Response:
            self.calls.append(url)
            return Response(self.status_code)

    sleeps: list[float] = []
    clock = iter([10.0, 10.0, 10.0, 10.0]).__next__
    underlying = Session()
    bounded = BoundedOfficialSession(
        maximum=2,
        min_interval_seconds=2,
        session=underlying,  # type: ignore[arg-type]
        monotonic=clock,
        sleep=sleeps.append,
    )
    bounded.get("https://official.example/data")
    bounded.get("https://official.example/data?page=2")
    assert bounded.request_count == 2
    assert sleeps == [2.0]
    with pytest.raises(PipelineAdapterError, match="official_source_request_budget_exceeded"):
        bounded.get("https://official.example/data?page=3")
    assert len(underlying.calls) == 2

    for status_code in (403, 429):
        blocked_session = Session(status_code=status_code)
        blocked = BoundedOfficialSession(
            maximum=2,
            min_interval_seconds=2,
            session=blocked_session,  # type: ignore[arg-type]
            monotonic=lambda: 0.0,
            sleep=lambda _delay: None,
        )
        with pytest.raises(PipelineAdapterError, match="official_source_unavailable"):
            blocked.get("https://official.example/data")
        assert blocked.request_count == 1
        assert len(blocked_session.calls) == 1


def test_bounded_official_session_counts_and_sanitizes_each_redirect_hop() -> None:
    class Response:
        def __init__(self, status_code: int, location: str | None = None) -> None:
            self.status_code = status_code
            self.headers = {"Location": location} if location else {}
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Session:
        headers = {"User-Agent": "test-agent"}
        auth = None

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.responses = [
                Response(302, "https://cdn.example/data.csv"),
                Response(200),
            ]

        def get(self, url: str, **kwargs: Any) -> Response:
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    underlying = Session()
    bounded = BoundedOfficialSession(
        maximum=2,
        min_interval_seconds=2,
        session=underlying,  # type: ignore[arg-type]
        monotonic=iter([0.0, 0.0, 0.0, 0.0]).__next__,
        sleep=lambda _delay: None,
    )
    response = bounded.get(
        "https://origin.example/export?id=1",
        auth=("account", "secret"),
        headers={"Authorization": "request-secret", "Accept": "text/csv"},
    )
    assert response.status_code == 200
    assert bounded.request_count == 2
    assert [call[0] for call in underlying.calls] == [
        "https://origin.example/export?id=1",
        "https://cdn.example/data.csv",
    ]
    assert all(call[1]["allow_redirects"] is False for call in underlying.calls)
    assert underlying.calls[0][1]["auth"] == ("account", "secret")
    redirected = underlying.calls[1][1]
    assert "auth" not in redirected
    assert redirected["headers"] == {"User-Agent": "test-agent", "Accept": "text/csv"}
    assert "params" not in redirected
    assert underlying.responses == []


def test_bounded_official_session_rejects_cross_origin_redirect_with_session_credentials() -> None:
    class Response:
        status_code = 302
        headers = {"Location": "https://cdn.example/data.csv"}

        def close(self) -> None:
            pass

    class Session:
        headers = {"Authorization": "session-secret"}
        auth = None

        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: Any) -> Response:
            self.calls += 1
            return Response()

    underlying = Session()
    bounded = BoundedOfficialSession(
        maximum=3,
        min_interval_seconds=2,
        session=underlying,  # type: ignore[arg-type]
        monotonic=lambda: 0.0,
        sleep=lambda _delay: None,
    )
    with pytest.raises(
        PipelineAdapterError,
        match="cross_origin_redirect_with_session_auth_forbidden",
    ):
        bounded.get("https://origin.example/export")
    assert bounded.request_count == 1
    assert underlying.calls == 1


def test_bounded_official_session_stops_when_redirect_exceeds_request_budget() -> None:
    class Response:
        status_code = 302
        headers = {"Location": "/next"}

    class Session:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: Any) -> Response:
            self.calls += 1
            return Response()

    underlying = Session()
    bounded = BoundedOfficialSession(
        maximum=1,
        min_interval_seconds=2,
        session=underlying,  # type: ignore[arg-type]
        monotonic=lambda: 0.0,
        sleep=lambda _delay: None,
    )
    with pytest.raises(PipelineAdapterError, match="official_source_request_budget_exceeded"):
        bounded.get("https://origin.example/export")
    assert bounded.request_count == 1
    assert underlying.calls == 1


def test_live_adapter_requires_external_authorization_before_workspace_write(tmp_path: Path) -> None:
    payload = _payload()
    adapter = CoreCollectionStageAdapter()
    with pytest.raises(PipelineRunnerError, match="live_authorization_verification_failed"):
        _run(payload, tmp_path, adapter=adapter, verifier=lambda _context: False)
    assert not (tmp_path / "state").exists()


def test_core_collection_adapter_runs_existing_collector_and_reuses_verified_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trading_desk.pipeline_adapters as adapters_module

    calls: list[dict[str, Any]] = []

    def fake_collect(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        assert kwargs["dates"] == CORE_CONFIG["dates"]
        assert kwargs["collection_window"] == "reconcile"
        assert kwargs["pcf_max_codes"] == 16
        assert kwargs["pcf_max_workers"] == 1
        assert isinstance(kwargs["session"], BoundedOfficialSession)
        assert kwargs["report_root"].is_relative_to(kwargs["workspace_root"])
        return _write_phase0_result(
            data_root=kwargs["data_root"],
            report_root=kwargs["report_root"],
            workspace_root=kwargs["workspace_root"],
        )

    monkeypatch.setattr(adapters_module, "collect_phase0", fake_collect)
    adapter = CoreCollectionStageAdapter(_sleep=lambda _delay: None, _monotonic=lambda: 0.0)
    report = _run(_payload(), tmp_path, adapter=adapter)
    assert report["status"] == "COMPLETED"
    assert report["executionMode"] == "stage_adapter"
    assert report["sideEffects"]["officialSourceRequests"] is False
    assert report["sideEffects"]["officialSourceRequestCount"] == 0
    assert report["sideEffects"]["officialSourceRequestsAllowed"] is True
    assert report["sideEffects"]["remoteWrites"] is False
    assert report["plan"]["records"][0]["reused"] is False
    assert len(calls) == 1

    resumed = _run(_payload(), tmp_path, adapter=adapter)
    assert resumed["status"] == "COMPLETED"
    assert resumed["plan"]["records"][0]["reused"] is True
    assert len(calls) == 1


def test_runner_reports_and_validates_source_request_evidence(tmp_path: Path) -> None:
    class RequestEvidenceAdapter:
        allowed_side_effects = (
            "official_source_requests",
            "filesystem:isolated_stage_workspace",
        )

        def __init__(self, observed: Any, budget: Any) -> None:
            self.observed = observed
            self.budget = budget

        def run(self, **_kwargs: Any) -> Mapping[str, Any]:
            return {
                "status": "COMPLETED",
                "outputSha256": "c" * 64,
                "allowedSideEffects": list(self.allowed_side_effects),
                "evidence": {
                    "sourceRequestCount": self.observed,
                    "sourceRequestBudget": self.budget,
                },
            }

        def verify_checkpoint(self, **_kwargs: Any) -> bool:
            return True

    valid = _run(
        _payload(),
        tmp_path / "valid",
        adapter=RequestEvidenceAdapter(3, 80),
    )
    assert valid["sideEffects"]["officialSourceRequests"] is True
    assert valid["sideEffects"]["officialSourceRequestCount"] == 3
    assert valid["sideEffects"]["officialSourceRequestsAllowed"] is True

    for index, (observed, budget) in enumerate(((None, 80), (81, 80), (True, 80))):
        with pytest.raises(PipelineRunnerError, match="stage_source_request_evidence_invalid"):
            _run(
                _payload(),
                tmp_path / "invalid" / str(index),
                adapter=RequestEvidenceAdapter(observed, budget),
            )


def test_core_collection_checkpoint_reuse_fails_closed_when_artifact_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trading_desk.pipeline_adapters as adapters_module

    def fake_collect(**kwargs: Any) -> dict[str, Any]:
        return _write_phase0_result(
            data_root=kwargs["data_root"],
            report_root=kwargs["report_root"],
            workspace_root=kwargs["workspace_root"],
        )

    monkeypatch.setattr(adapters_module, "collect_phase0", fake_collect)
    adapter = CoreCollectionStageAdapter(_sleep=lambda _delay: None, _monotonic=lambda: 0.0)
    first = _run(_payload(), tmp_path, adapter=adapter)
    input_sha = first["plan"]["checkpoints"][0]["inputSha256"]
    manifest = tmp_path / "state" / "stages" / "core_collection" / input_sha / "phase0_reports" / "latest-manifest.json"
    manifest.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(PipelineRunnerError, match="stage_checkpoint_evidence_mismatch"):
        _run(_payload(), tmp_path, adapter=adapter)


def test_partial_collection_workspace_is_not_retried_and_inputs_are_strict(tmp_path: Path) -> None:
    adapter = CoreCollectionStageAdapter()
    bad_config = {**CORE_CONFIG, "dates": ["2026-09-28", "2026-09-25", "2026-09-29"]}
    payload = _payload(bad_config)
    with pytest.raises(PipelineRunnerError, match="core_collection_dates_must_be_ascending"):
        _run(payload, tmp_path / "bad", adapter=adapter)

    workspace = tmp_path / "partial" / "state" / "stages" / "core_collection" / canonical_sha256(CORE_CONFIG)
    workspace.mkdir(parents=True)
    (workspace / "partial.csv").write_text("partial", encoding="utf-8")
    with pytest.raises(PipelineRunnerError, match="partial_core_collection_requires_reconciliation"):
        _run(_payload(), tmp_path / "partial", adapter=adapter)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("maxRequests", 101, "invalid_source_request_budget"),
        ("pcfMaxCodes", 17, "invalid_core_collection_pcf_limit"),
        ("minIntervalSeconds", 1.99, "invalid_source_request_interval"),
        ("sourceFingerprint", "not-a-sha", "invalid_source_fingerprint"),
    ],
)
def test_core_collection_adapter_enforces_resource_and_identity_caps(
    tmp_path: Path, field: str, value: Any, error: str
) -> None:
    config = {**CORE_CONFIG, field: value}
    with pytest.raises(PipelineRunnerError, match=error):
        _run(_payload(config), tmp_path, adapter=CoreCollectionStageAdapter())
    assert not (tmp_path / "state").exists()


def test_phase0_collection_stops_after_official_source_429(tmp_path: Path) -> None:
    from trading_desk.phase0 import collect_phase0

    class Response:
        status_code = 429

    class Session:
        headers: dict[str, str] = {}
        request_timeout = (1.0, 1.0)

        def __init__(self) -> None:
            self.calls: list[str] = []

        def get(self, url: str, **_kwargs: Any) -> Response:
            self.calls.append(url)
            return Response()

    underlying = Session()
    bounded = BoundedOfficialSession(
        maximum=10,
        min_interval_seconds=2,
        session=underlying,  # type: ignore[arg-type]
        monotonic=lambda: 0.0,
        sleep=lambda _delay: None,
    )
    with pytest.raises(PipelineAdapterError, match="official_source_unavailable"):
        collect_phase0(
            dates=list(CORE_CONFIG["dates"]),
            data_root=tmp_path / "data",
            report_root=tmp_path / "phase0_reports",
            workspace_root=tmp_path,
            collection_window="reconcile",
            pcf_max_codes=0,
            session=bounded,  # type: ignore[arg-type]
            pcf_max_workers=1,
        )
    assert bounded.request_count == 1
    assert len(underlying.calls) == 1
    assert not (tmp_path / "phase0_reports").exists()
