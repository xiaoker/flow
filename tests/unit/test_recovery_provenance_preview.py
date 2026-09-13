from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from scripts.preview_recovery_provenance import (
    CONTRACT,
    PREVIEW_CONTRACT,
    ProvenancePreviewError,
    export_preview,
)


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/recovery-provenance-preview.yml"
SHA_A = "a" * 64
SHA_B = "b" * 64


def page(*, cursor: str | None, next_cursor: str | None, has_more: bool, plan_hash: str, status: str = "READY_TO_APPLY_METADATA_ONLY") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "contract": CONTRACT,
        "scope": {"start": "2024-08-01", "end": "2026-09-03"},
        "request": {"start": "2024-08-01", "end": "2026-09-03", "cursor": cursor, "limit": 4},
        "status": status,
        "planHash": plan_hash,
        "planStatus": "NOT_APPROVED",
        "reconciliationStatus": "PENDING",
        "executionBlocked": True,
        "budgetStatus": "unknown",
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "d1": {"queryCount": 1, "rowsRead": 2, "rowsWritten": 0},
        "r2": {"gets": 1, "heads": 3, "bytesRead": 12, "listCalls": 0, "rawBodiesRead": 0, "normalizedBodiesRead": 0},
        "issues": [],
    }


class FakeResponse:
    def __init__(self, value: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


def test_preview_paginates_get_only_and_writes_bounded_artifacts(tmp_path: Path) -> None:
    responses = [
        FakeResponse(page(cursor=None, next_cursor="2026-08-27", has_more=True, plan_hash=SHA_A)),
        FakeResponse(page(cursor="2026-08-27", next_cursor=None, has_more=False, plan_hash=SHA_B, status="INCOMPLETE")),
    ]
    requests: list[Any] = []

    def opener(request: Any, *, timeout: int) -> FakeResponse:
        requests.append((request, timeout))
        return responses.pop(0)

    summary = export_preview(
        base_url="https://flow.test",
        api_key="do-not-write-this-to-artifacts",
        start="2024-08-01",
        end="2026-09-03",
        limit=4,
        output_dir=tmp_path / "preview",
        opener=opener,
    )
    assert summary["contract"] == PREVIEW_CONTRACT
    assert summary["status"] == "INCOMPLETE"
    assert summary["pageCount"] == 2
    assert summary["executionBlocked"] is True
    assert summary["safety"] == {
        "httpMethods": ["GET"],
        "d1RowsWritten": 0,
        "r2Writes": 0,
        "workflowDispatches": 0,
        "activePointerChanges": 0,
    }
    assert [request.get_method() for request, _ in requests] == ["GET", "GET"]
    assert all(request.get_header("Authorization") == "Bearer do-not-write-this-to-artifacts" for request, _ in requests)
    assert (tmp_path / "preview/page-001.json").is_file()
    assert (tmp_path / "preview/page-002.json").is_file()
    assert (tmp_path / "preview/provenance-preview.json").is_file()
    output_text = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "preview").glob("*.json"))
    assert "do-not-write-this-to-artifacts" not in output_text


def test_preview_fails_closed_on_http_error_and_writes_safe_failure(tmp_path: Path) -> None:
    def opener(request: Any, *, timeout: int) -> Any:
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    with pytest.raises(ProvenancePreviewError, match="http_503"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-01",
            end="2026-09-03",
            limit=4,
            output_dir=tmp_path / "preview",
            opener=opener,
        )


def test_preview_rejects_non_advancing_cursor(tmp_path: Path) -> None:
    responses = [
        FakeResponse(page(cursor=None, next_cursor="2024-08-01", has_more=True, plan_hash=SHA_A)),
        FakeResponse(page(cursor="2024-08-01", next_cursor="2024-08-01", has_more=True, plan_hash=SHA_B)),
    ]

    with pytest.raises(ProvenancePreviewError, match="pagination_cursor_not_advancing"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-01",
            end="2026-09-03",
            limit=4,
            output_dir=tmp_path / "preview",
            opener=lambda request, *, timeout: responses.pop(0),
        )


def test_preview_rejects_protected_output_paths(tmp_path: Path) -> None:
    with pytest.raises(ProvenancePreviewError, match="protected_output_path"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-01",
            end="2026-09-03",
            limit=4,
            output_dir=tmp_path / "memory" / "preview",
            opener=lambda request, *, timeout: None,
        )


def test_preview_does_not_overwrite_an_existing_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "preview"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ProvenancePreviewError, match="output_dir_not_empty"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-01",
            end="2026-09-03",
            limit=4,
            output_dir=output,
            opener=lambda request, *, timeout: None,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_preview_contract_requires_fixed_scope_and_bounded_limit(tmp_path: Path) -> None:
    with pytest.raises(ProvenancePreviewError, match="fixed_scope_required"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-02",
            end="2026-09-03",
            limit=4,
            output_dir=tmp_path / "preview",
        )
    with pytest.raises(ProvenancePreviewError, match="invalid_limit"):
        export_preview(
            base_url="https://flow.test",
            api_key="secret",
            start="2024-08-01",
            end="2026-09-03",
            limit=5,
            output_dir=tmp_path / "preview",
        )


def test_workflow_is_manual_bounded_and_read_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text
    assert "contents: read" in text
    assert "group: recovery-provenance-preview" in text
    assert "cancel-in-progress: false" in text
    assert "scripts/preview_recovery_provenance.py" in text
    assert "if: ${{ always() }}" in text
    assert "actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f" in text
    assert "FLOW_INGEST_API_KEY: ${{ secrets.FLOW_INGEST_API_KEY }}" in text
    assert "FLOW_CF_ACCESS_CLIENT_SECRET: ${{ secrets.FLOW_CF_ACCESS_CLIENT_SECRET }}" in text
    for forbidden in ("market-data-collect", "market-data-publish", "Watchdog", "Collector", "wrangler", "PIPELINE_RECOVERY_ENABLED", "-X POST", "POST /api"):
        assert forbidden not in text


def test_preview_module_has_no_mutation_or_dispatch_capability() -> None:
    source = (ROOT / "scripts/preview_recovery_provenance.py").read_text(encoding="utf-8")
    assert "urllib.request.Request" in source
    assert "method=\"GET\"" in source
    assert "subprocess" not in source
    assert "workflow_dispatch(" not in source
    assert "POST" not in source
    assert "INSERT" not in source
    assert "UPDATE" not in source
    assert "TRUNCATE" not in source
    assert "workflow_dispatch(" not in source
