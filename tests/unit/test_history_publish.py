from __future__ import annotations

import json
from unittest.mock import Mock

from trading_desk import history_publish


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
