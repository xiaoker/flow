from __future__ import annotations

import base64
import gzip
import hashlib

import pytest

from scripts.publish_read_models import build_upload_body, check_capabilities
from scripts.verify_published_release import CANDIDATE_ARTIFACTS, slot_contract, verify, verify_candidate


def _manifest() -> dict:
    return {
        "schemaVersion": 3,
        "releaseId": "release:2026-08-13:0123456789abcdef",
        "asOf": "2026-08-13",
        "observedAt": "2026-08-14T03:17:42.000Z",
        "artifacts": {
            "overview": {
                "key": "market-data/releases/2026-08-13/release:2026-08-13:0123456789abcdef/overview.json",
                "sha256": "0" * 64,
                "bytes": 5,
            }
        },
    }


def test_stage_only_upload_is_explicit_and_round_trips_compressed_content() -> None:
    content = b'{"asOf":"2026-08-13"}'
    manifest = _manifest()
    declared = {
        **manifest["artifacts"]["overview"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }

    body = build_upload_body(
        manifest,
        "overview",
        declared,
        content,
        finalize=True,
        activation_mode="stage_only",
    )

    assert body["activationMode"] == "stage_only"
    assert body["finalize"] is True
    assert gzip.decompress(base64.b64decode(body["artifact"]["contentBase64"])) == content
    assert body["artifact"]["sha256"] == declared["sha256"]


def test_upload_defaults_to_active_mode_for_existing_workflows() -> None:
    content = b"{}"
    manifest = _manifest()
    declared = {
        **manifest["artifacts"]["overview"],
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }

    body = build_upload_body(manifest, "overview", declared, content, finalize=False)

    assert body["activationMode"] == "active"
    assert body["finalize"] is False


def test_upload_rejects_unknown_activation_mode() -> None:
    with pytest.raises(ValueError, match="unsupported activation mode"):
        build_upload_body(
            _manifest(),
            "overview",
            _manifest()["artifacts"]["overview"],
            b"{}",
            finalize=False,
            activation_mode="activate_and_pray",
        )


def test_dashboard_capability_does_not_require_pcf_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "readModelManifestSchemas": [2, 3],
        "schemaVersions": [2, 3],
        "pressureSchemaVersions": [2],
        "stockShardCounts": [8],
        "maxArtifacts": 20,
        "maxArtifactBytes": 5_000_000,
        "features": ["requiredArtifacts", "sameDateObservedAtOrder", "releaseSlots", "bseMarginSeparateScope"],
        "migration": {
            "releaseObservedAtColumns": True,
            "sourceReadinessLedger": False,
            "marketHistoryBseColumn": True,
        },
        "marketHistoryScopeVersion": "sse-szse-bse-v1",
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return payload

    module = __import__("scripts.publish_read_models", fromlist=["requests"])
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: FakeResponse())
    check_capabilities("https://flow.example/capabilities", {"Content-Type": "application/json"}, slot="dashboard")
    with pytest.raises(RuntimeError, match="deployed capability lacks v3 release features"):
        check_capabilities("https://flow.example/capabilities", {"Content-Type": "application/json"}, slot="etf_penetration")


def test_dashboard_canary_contract_only_checks_market_artifacts() -> None:
    views, artifacts = slot_contract("dashboard")
    assert views == ("overview", "radar", "rotation", "national", "connect", "margin")
    assert artifacts == (
        "bootstrap",
        "overview",
        "radar",
        "rotation",
        "national",
        "connect",
        "margin",
    )


def test_candidate_canary_reads_each_explicit_staged_artifact_without_active_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOW_INGEST_API_KEY", "ingest-key")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "access-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "access-secret")
    module = __import__("scripts.verify_published_release", fromlist=["requests"])
    calls: list[dict] = []

    class FakeResponse:
        headers = {
            "x-flow-release-id": "release:2026-08-13:0123456789abcdef",
            "x-flow-as-of": "2026-08-13",
            "x-flow-release-status": "STAGED",
        }

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"asOf": "2026-08-13"}

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "get", fake_get)
    result = verify_candidate(
        "https://flow.example",
        expected_release_id="release:2026-08-13:0123456789abcdef",
        expected_as_of="2026-08-13",
        timeout=1.0,
    )

    assert result["mode"] == "candidate"
    assert result["checkedArtifacts"] == len(CANDIDATE_ARTIFACTS) == 18
    assert len(calls) == 18
    assert all(call["params"]["candidate"] == "1" for call in calls)
    assert all(call["headers"]["Authorization"] == "Bearer ingest-key" for call in calls)


def test_active_canary_repeats_every_read_and_reports_request_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "access-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "access-secret")
    module = __import__("scripts.verify_published_release", fromlist=["requests"])
    calls: list[dict] = []

    class FakeResponse:
        headers: dict[str, str] = {
            "x-flow-release-id": "release:2026-08-13:0123456789abcdef",
        }
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "releaseId": "release:2026-08-13:0123456789abcdef",
                "asOf": "2026-08-13",
                "observedAt": "2026-08-14T03:17:42.000Z",
            }

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "get", fake_get)
    result = verify(
        "https://flow.example",
        expected_release_id="release:2026-08-13:0123456789abcdef",
        expected_as_of="2026-08-13",
        repeat=3,
        timeout=1.0,
    )

    assert result["iterations"] == 3
    assert result["checkedViews"] == 8
    assert result["requests"] == 42
    assert len(calls) == result["requests"]


def test_active_canary_rejects_an_unbounded_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "access-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "access-secret")
    with pytest.raises(ValueError, match="between 1 and 100"):
        verify(
            "https://flow.example",
            expected_release_id="release:2026-08-13:0123456789abcdef",
            expected_as_of="2026-08-13",
            repeat=101,
        )
