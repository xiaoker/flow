from __future__ import annotations

import json
from copy import deepcopy
import pytest
from unittest.mock import Mock

from trading_desk import history_publish


def split_bundle():
    return {"version": 1, "history": {"runId": "history-test", "runKey": "key-one",
        "asOf": "2026-07-31", "finalize": False, "artifacts": [
            {"releaseId": "etf_scale:sse:2026-07-31:aaa"},
            {"releaseId": "etf_scale:szse:2026-07-31:bbb"}]}}


def test_split_source_preservation_then_finalize_has_same_request_count(monkeypatch):
    calls = []
    def post(_url, *, data, **_kwargs):
        body = json.loads(data); calls.append(body)
        result = Mock(ok=True)
        result.json.return_value = {"status": "archived" if body["history"]["finalize"] else "chunk_archived"}
        return result
    monkeypatch.setattr(history_publish.requests, "post", post)
    checkpoint = split_bundle()
    options = dict(url="https://flow.example/history", ingest_key="fixture",
                   access_client_id="fixture", access_client_secret="fixture")
    result = history_publish.publish_history_bundle(bundle=checkpoint, checkpoint_only=True, **options)
    assert result["status"] == "chunk_archived"
    assert len(calls) == 2 and all(not c["history"]["finalize"] for c in calls)
    assert all("snapshot" not in c["history"] for c in calls)
    # Downstream computation may fail here: these remain real source receipts,
    # not a fabricated finalization or active-pointer operation.
    final = deepcopy(checkpoint)
    final["history"]["snapshot"] = {"asOf": "2026-07-31"}
    history_publish.publish_history_bundle(bundle=final, finalize_only=True, checkpoint_bundle=checkpoint, **options)
    assert len(calls) == 3 and calls[-1]["history"]["finalize"] is True
    assert calls[-1]["history"]["expectedArtifacts"] == [a["releaseId"] for a in checkpoint["history"]["artifacts"]]
    with pytest.raises(ValueError, match="mutually exclusive"):
        history_publish.publish_history_bundle(bundle=final, checkpoint_only=True, finalize_only=True, **options)
    assert len(calls) == 3


@pytest.mark.parametrize("field", ["runId", "runKey", "asOf", "artifacts"])
def test_finalize_rejects_changed_checkpoint_identity_before_http(monkeypatch, field):
    checkpoint = split_bundle(); final = deepcopy(checkpoint)
    final["history"]["snapshot"] = {"asOf": "2026-07-31"}
    final["history"][field] = [] if field == "artifacts" else "changed"
    post = Mock(); monkeypatch.setattr(history_publish.requests, "post", post)
    with pytest.raises(ValueError, match="source checkpoint identity mismatch"):
        history_publish.publish_history_bundle(url="https://flow.example/history", bundle=final,
            ingest_key="fixture", access_client_id="fixture", access_client_secret="fixture",
            finalize_only=True, checkpoint_bundle=checkpoint)
    post.assert_not_called()


@pytest.mark.parametrize("fault", ["bad_receipt", "missing_snapshot", "ambiguous_response", "missing_checkpoint"])
def test_source_preservation_errors_never_finalize(monkeypatch, fault):
    calls = []
    def post(_url, *, data, **_kwargs):
        calls.append(json.loads(data))
        if fault == "ambiguous_response":
            raise TimeoutError("fixture response lost")
        result = Mock(ok=True); result.json.return_value = {"status": "waiting_sources"}
        return result
    monkeypatch.setattr(history_publish.requests, "post", post)
    bundle = split_bundle()
    if fault == "missing_checkpoint": bundle["history"]["snapshot"] = {"asOf": "2026-07-31"}
    with pytest.raises((ValueError, RuntimeError)):
        history_publish.publish_history_bundle(bundle=bundle, url="https://flow.example/history",
            ingest_key="fixture", access_client_id="fixture", access_client_secret="fixture", max_attempts=1,
            checkpoint_only=fault not in {"missing_snapshot", "missing_checkpoint"},
            finalize_only=fault in {"missing_snapshot", "missing_checkpoint"})
    assert all(c["history"]["finalize"] is False and "snapshot" not in c["history"] for c in calls)
    assert len(calls) == (0 if fault in {"missing_snapshot", "missing_checkpoint"} else 1)


def test_pending_fallback_replays_exact_sources_under_existing_final_envelope(tmp_path, monkeypatch):
    from test_history_bundle import test_history_bundle_preserves_raw_and_normalized_hashes
    from trading_desk.history_bundle import build_history_bundle_from_files, build_source_history_bundle_from_files
    test_history_bundle_preserves_raw_and_normalized_hashes(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes()); manifest["requested_dates"] = ["2026-07-20", "2026-07-21"]
    manifest_path.write_text(json.dumps(manifest))
    checkpoint = build_source_history_bundle_from_files(manifest_path=manifest_path, workspace_root=tmp_path)
    (tmp_path / "snapshot.json").write_text(json.dumps({"asOf": "2026-07-20",
        "publication": {"status": "pending", "targetAsOf": "2026-07-21"}}))
    final = build_history_bundle_from_files(snapshot_path=tmp_path / "snapshot.json",
        manifest_path=manifest_path, workspace_root=tmp_path)
    calls = []
    def post(_url, *, data, **_kwargs):
        body = json.loads(data); calls.append(body)
        response = Mock(ok=True)
        response.json.return_value = {"status": "archived_pending" if body["history"]["finalize"] else "chunk_archived"}
        return response
    monkeypatch.setattr(history_publish.requests, "post", post)
    options = dict(url="https://flow.example/history", ingest_key="fixture", access_client_id="fixture", access_client_secret="fixture")
    history_publish.publish_history_bundle(bundle=checkpoint, checkpoint_only=True, **options)
    result = history_publish.publish_history_bundle(bundle=final, finalize_only=True, checkpoint_bundle=checkpoint, **options)
    assert result["status"] == "archived_pending"
    assert len(calls) == 3  # N early + N exact replays + one final request
    assert [c["history"]["asOf"] for c in calls] == ["2026-07-21", "2026-07-20", "2026-07-20"]
    assert calls[0]["history"]["artifacts"] == calls[1]["history"]["artifacts"] == calls[2]["history"]["artifacts"]
    assert [c["history"]["finalize"] for c in calls] == [False, False, True]


def test_history_publish_uploads_individual_checkpoints_before_finalize(monkeypatch) -> None:
    responses = []

    def post(_url, *, data, headers, timeout):
        responses.append((json.loads(data), headers, timeout))
        result = Mock()
        result.ok = True
        result.json.return_value = {"status": "archived"}
        return result

    monkeypatch.setattr(history_publish.requests, "post", post)
    bundle = {
        "version": 1,
        "history": {
            "runId": "history-test",
            "startedAt": "2026-08-01T18:30:00+08:00",
            "finishedAt": "2026-08-01T18:31:00+08:00",
            "retentionDays": 0,
            "asOf": "2026-07-31",
            "snapshot": {"asOf": "2026-07-31"},
            "artifacts": [
                {"releaseId": "etf_scale:sse:2026-07-31:aaa"},
                {"releaseId": "margin_market_daily:sse:2026-07-31:bbb"},
            ],
        },
    }

    result = history_publish.publish_history_bundle(
        url="https://flow.example/api/v1/admin/history-import",
        bundle=bundle,
        ingest_key="ingest",
        access_client_id="client",
        access_client_secret="secret",
    )

    assert result == {"status": "archived"}
    assert len(responses) == 3
    assert [
        request[0]["history"]["finalize"] for request in responses
    ] == [False, False, True]
    assert "snapshot" not in responses[0][0]["history"]
    assert "snapshot" not in responses[1][0]["history"]
    assert responses[2][0]["history"]["snapshot"] == {"asOf": "2026-07-31"}
    assert all(
        request[0]["history"]["expectedArtifacts"]
        == ["etf_scale:sse:2026-07-31:aaa", "margin_market_daily:sse:2026-07-31:bbb"]
        for request in responses
    )


def test_history_publish_sends_adjacent_contract_only_at_final_boundary(monkeypatch) -> None:
    requests = []

    def post(_url, *, data, headers, timeout):
        requests.append(json.loads(data))
        result = Mock()
        result.ok = True
        result.json.return_value = {"status": "archived"}
        return result

    monkeypatch.setattr(history_publish.requests, "post", post)
    contract = {
        "schemaVersion": 1,
        "asOf": "2026-07-31",
        "expectedPreviousDate": "2026-07-30",
        "sourceReleaseIds": ["etf_scale:sse:2026-07-31:aaa"],
        "inputSha256": "a" * 64,
        "rows": [],
    }
    bundle = {
        "version": 1,
        "history": {
            "runId": "history-adjacent",
            "startedAt": "2026-08-01T18:30:00+08:00",
            "finishedAt": "2026-08-01T18:31:00+08:00",
            "retentionDays": 0,
            "asOf": "2026-07-31",
            "officialEtfDailyChanges": contract,
            "snapshot": {"asOf": "2026-07-31"},
            "artifacts": [
                {"releaseId": "etf_scale:sse:2026-07-31:aaa"},
                {"releaseId": "margin_market_daily:sse:2026-07-31:bbb"},
            ],
        },
    }

    history_publish.publish_history_bundle(
        url="https://flow.example/api/v1/admin/history-import",
        bundle=bundle,
        ingest_key="ingest",
        access_client_id="client",
        access_client_secret="secret",
    )

    assert all("officialEtfDailyChanges" not in item["history"] for item in requests[:-1])
    assert requests[-1]["history"]["officialEtfDailyChanges"] == contract
