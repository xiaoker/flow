from __future__ import annotations

import base64
import gzip
import hashlib

import pytest

from scripts.publish_read_models import build_upload_body
from scripts.activate_read_models import activate
from scripts.verify_published_release import CANDIDATE_ARTIFACTS, verify_candidate


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


def test_candidate_activation_requires_matching_release_and_accepts_idempotent_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOW_INGEST_API_KEY", "ingest-key")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_ID", "access-id")
    monkeypatch.setenv("FLOW_CF_ACCESS_CLIENT_SECRET", "access-secret")
    module = __import__("scripts.activate_read_models", fromlist=["requests"])

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {
                "status": "activated",
                "releaseId": "release:2026-08-13:0123456789abcdef",
                "asOf": "2026-08-13",
            }

    calls: list[dict] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    result = activate(
        "https://flow.example",
        release_id="release:2026-08-13:0123456789abcdef",
        as_of="2026-08-13",
    )

    assert result["status"] == "activated"
    assert calls[0]["url"].endswith("/api/v1/admin/read-model-activate")
    assert calls[0]["json"] == {
        "releaseId": "release:2026-08-13:0123456789abcdef",
        "asOf": "2026-08-13",
    }
