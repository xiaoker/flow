"""Exercise the actual publish shell boundary through canonical receipt creation."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import textwrap
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts import create_live_recovery_receipts as receipts
from trading_desk.recovery_execution import (
    RecoveryContractError, build_recovery_plan, validate_recovery_plan,
    validate_recovery_receipt,
)

ROOT = Path(__file__).resolve().parents[2]


def _plan() -> dict:
    return build_recovery_plan(
        campaign_id="receipt-boundary", recovery_id="receipt-boundary-16",
        run_key="daily-market-data:2026-10-16:evening:v1",
        data_dates=["2026-10-16"], required_stages=["dashboard", "publish"],
        artifact_root=".recovery/receipt-boundary-16", state_dir=".recovery/receipt-boundary-16/state",
        max_dates=1, max_requests=194, deadline="2026-12-31T00:00:00Z",
        budget_manifest={"source": "offline-fixture", "measured": True},
    )


def _envelope(plan: dict) -> dict:
    return {"recoveryId": plan["recoveryId"], "planSha256": plan["planHash"],
            "scopeSha256": "a" * 64, "planKey": "recovery/plans/fixture.json", "plan": plan}


@contextmanager
def _api(value: dict, status: int = 200, *, recovery_id: str = "receipt-boundary-16"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == f"/recovery/plan?recoveryId={recovery_id}"
            assert self.headers["Authorization"] == "Bearer fixture-key"
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _materialize(tmp_path: Path, plan: dict, response: dict, *, status: int = 200):
    workflow = (ROOT / ".github/workflows/market-data-publish.yml").read_text()
    step = workflow.split("      - name: Materialize approved recovery execution contract\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    with _api(response, status, recovery_id=plan["recoveryId"]) as origin:
        environment = {**os.environ, "FLOW_PIPELINE_WINDOW_URL": origin,
            "FLOW_INGEST_API_KEY": "fixture-key", "FLOW_CF_ACCESS_CLIENT_ID": "fixture-id",
            "FLOW_CF_ACCESS_CLIENT_SECRET": "fixture-secret", "INPUT_RECOVERY_ID": plan["recoveryId"],
            "INPUT_RUN_KEY": plan["runKey"], "INPUT_RECOVERY_DATA_DATE": "2026-10-16",
            "INPUT_RECOVERY_STAGE": "publish", "INPUT_RECOVERY_PLAN_SHA256": plan["planHash"]}
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=environment,
                              capture_output=True, text=True, timeout=10)


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _receipt_inputs(tmp_path: Path, plan: dict) -> list[str]:
    source = tmp_path / "source"
    _write(source / "recovery-evidence.json", {
        "files": {"dashboard-snapshot.json": {"sha256": "b" * 64}},
        "collectorRunId": "collector-fixture-16", "sourceFingerprint": "c" * 64,
        "contextDates": ["2026-10-15", "2026-10-16"],
        "historyArchive": {"snapshotKey": "market-data/snapshots/2026-10-16/fixture.json", "snapshotSha256": "d" * 64}})
    _write(tmp_path / "read-models-dashboard/manifest.json", {
        "asOf": "2026-10-16", "releaseId": "release:2026-10-16:fixture", "manifestSha256": "e" * 64})
    _write(source / "previous-release.json", {"releaseId": "release:2026-10-29:current"})
    _write(tmp_path / "activation.json", {"status": "staged_stale_revision", "releaseId": "release:2026-10-16:fixture"})
    _write(tmp_path / "metrics.json", {"measurementStatus": "MEASURED", "d1RowsRead": 7,
        "d1RowsWritten": 6, "workerRequests": 96, "r2ClassA": 8, "r2ClassB": 7,
        "r2Bytes": 1000, "actionsMinutes": 1, "measuredAt": "2026-10-30T16:28:57Z"})
    return ["receipt", "--plan", str(tmp_path / "recovery-execution.json"),
        "--expected-plan-hash", plan["planHash"], "--attempt", "4", "--producer-build-sha", "f" * 64,
        "--source-dir", str(source), "--models-dir", str(tmp_path), "--evidence-dir", str(tmp_path),
        "--resource-metrics", str(tmp_path / "metrics.json"), "--publish-run-id", "publish-fixture-16",
        "--active-before", str(source / "previous-release.json"), "--active-after", str(tmp_path / "activation.json"),
        "--output-dir", str(tmp_path / ".recovery/receipts")]


def test_actual_workflow_response_to_receipt_boundary(tmp_path: Path, monkeypatch):
    plan = _plan()
    result = _materialize(tmp_path, plan, _envelope(plan))
    assert result.returncode == 0, result.stderr
    monkeypatch.setattr(sys, "argv", _receipt_inputs(tmp_path, plan))
    receipts.main()
    assert json.loads((tmp_path / "recovery-execution.json").read_text()) == plan
    scope = validate_recovery_plan(plan, expected_plan_hash=plan["planHash"])
    for stage in ["dashboard", "publish"]:
        receipt = json.loads((tmp_path / f".recovery/receipts/{stage}.json").read_text())
        validate_recovery_receipt(receipt, scope=scope, expected_attempt=4)
        assert receipt["historical"]["activeBefore"] == receipt["historical"]["activeAfter"] == "release:2026-10-29:current"
        assert receipt["output"]["asOf"] == "2026-10-16"


@pytest.mark.parametrize("fault", ["http", "id", "outer_hash", "inner_hash", "run_key", "date", "stage", "missing_plan"])
def test_workflow_rejects_unbound_response(tmp_path: Path, fault: str):
    plan = _plan(); response = copy.deepcopy(_envelope(plan))
    if fault == "id": response["recoveryId"] = "other"
    if fault == "outer_hash": response["planSha256"] = "0" * 64
    if fault == "inner_hash": response["plan"]["planHash"] = "0" * 64
    if fault == "run_key": response["plan"]["runKey"] = "daily-market-data:2026-10-19:evening:v1"
    if fault == "date": response["plan"]["dataDates"] = ["2026-10-19"]
    if fault == "stage": response["plan"]["requiredStages"] = ["dashboard"]
    if fault == "missing_plan": response.pop("plan")
    result = _materialize(tmp_path, plan, response, status=503 if fault == "http" else 200)
    assert result.returncode != 0
    assert not (tmp_path / "recovery-execution.json").exists()


def test_receipt_still_rejects_semantic_plan_tampering(tmp_path: Path, monkeypatch):
    plan = _plan()
    assert _materialize(tmp_path, plan, _envelope(plan)).returncode == 0
    altered = copy.deepcopy(plan); altered["maxRequests"] += 1
    _write(tmp_path / "recovery-execution.json", altered)
    monkeypatch.setattr(sys, "argv", _receipt_inputs(tmp_path, plan))
    with pytest.raises(RecoveryContractError, match="plan_hash_mismatch"):
        receipts.main()
    assert not (tmp_path / ".recovery/receipts").exists()
