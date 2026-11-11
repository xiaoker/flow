import hashlib
import json
from copy import deepcopy

import pytest

from scripts.build_phase0_archive_recipe import ArchiveReadError
from scripts import verify_etf_archive_partitions as verifier
from scripts.verify_etf_archive_partitions import CONTRACT, ArchiveVerificationError, verify


DATA = b"trade_date,market,instrument_code,shares\n2026-10-14,SZSE,159001,100\n"
KEY = "market-data/normalized/szse/etf_scale/2026-10-14/" + "a" * 64 + "/data.csv"


def request():
    return {"contract": CONTRACT, "objects": [{"objectKey": KEY, "sha256": hashlib.sha256(DATA).hexdigest()}]}


def test_exact_partition_read_and_honest_claims():
    calls = []
    def read(key, digest):
        calls.append((key, digest))
        return DATA
    result = verify(request(), read=read)
    assert len(calls) == result["archiveReadCount"] == 1
    assert result["objects"][0]["rows"] == 1
    assert not result["provesOfficialProvenance"]
    assert not result["provesPeriodRecovery"]
    assert not any(result["sideEffects"].values())


@pytest.mark.parametrize("mutation", ["duplicate", "overflow", "path", "hash", "date", "contract"])
def test_scope_rejected_before_network(mutation):
    value = deepcopy(request())
    if mutation == "duplicate": value["objects"] *= 2
    if mutation == "overflow": value["objects"] *= 9
    if mutation == "path": value["objects"][0]["objectKey"] = "https://other.invalid/secret"
    if mutation == "hash": value["objects"][0]["sha256"] = "invalid"
    if mutation == "date": value["objects"][0]["objectKey"] = KEY.replace("2026-10-14", "2026-02-30")
    if mutation == "contract": value["contract"] = "unknown"
    with pytest.raises(ValueError):
        verify(value, read=lambda *_: pytest.fail("network must not run"))


def test_bytes_or_row_identity_conflict_rejected():
    with pytest.raises(ValueError): verify(request(), read=lambda *_: DATA + b"x")
    value = request()
    bad = DATA.replace(b"2026-10-14", b"2026-10-13")
    value["objects"][0]["sha256"] = hashlib.sha256(bad).hexdigest()
    with pytest.raises(ValueError): verify(value, read=lambda *_: bad)


def test_http_failure_is_not_retried():
    calls = []
    def read(*args):
        calls.append(args)
        raise ArchiveReadError("archive_recipe_remote_http_error", status=404, ray="ray-test-123")
    with pytest.raises(ArchiveVerificationError) as captured:
        verify(request(), read=read)
    assert len(calls) == 1
    assert captured.value.receipt["failure"] == {
        "index": 1, "objectKey": KEY, "errorCode": "archive_recipe_remote_http_error",
        "httpStatus": 404, "rayId": "ray-test-123",
    }
    assert captured.value.receipt["archiveReadCount"] == 0
    assert not any(captured.value.receipt["sideEffects"].values())


def test_failure_receipt_retains_prior_success_and_identifies_failed_index():
    first_key = KEY.replace("szse", "sse")
    first_data = DATA.replace(b"SZSE", b"SSE")
    calls = []
    def read(key, _digest):
        calls.append(key)
        if key == KEY:
            raise ArchiveReadError("archive_recipe_remote_http_error", status=404)
        return first_data
    value = {"contract": CONTRACT, "objects": [
        {"objectKey": KEY, "sha256": hashlib.sha256(DATA).hexdigest()},
        {"objectKey": first_key, "sha256": hashlib.sha256(first_data).hexdigest()},
    ]}
    with pytest.raises(ArchiveVerificationError) as captured:
        verify(value, read=read)
    receipt = captured.value.receipt
    assert len(calls) == 2
    assert receipt["archiveReadCount"] == 1
    assert receipt["objects"][0]["objectKey"] == first_key
    assert receipt["failure"]["index"] == 2
    assert receipt["failure"]["objectKey"] == KEY
    assert receipt["failure"]["httpStatus"] == 404


def test_cli_persists_safe_failure_receipt_and_stays_failed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FLOW_ETF_ARCHIVE_REFS_JSON", json.dumps(request()))
    monkeypatch.setattr(verifier, "_archive_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ArchiveReadError("archive_recipe_remote_http_error", status=403, ray="ray-safe-456")))
    output = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["verify_etf_archive_partitions.py", "--output", str(output)])
    with pytest.raises(SystemExit) as captured:
        verifier.main()
    assert captured.value.code == 1
    receipt = json.loads(output.read_text())
    assert receipt["failure"]["httpStatus"] == 403
    assert receipt["failure"]["rayId"] == "ray-safe-456"
    assert receipt["status"] == "ARCHIVE_VERIFICATION_FAILED"
    assert "403" in capsys.readouterr().out


def test_actual_get_transport_and_parser(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from scripts import build_phase0_archive_recipe as transport
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.send_header("x-flow-archive-key", KEY)
            self.send_header("x-flow-archive-sha256", hashlib.sha256(DATA).hexdigest())
            self.end_headers()
            self.wfile.write(DATA)
        def log_message(self, *_): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(transport, "ARCHIVE_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-only")
    try:
        assert verify(request())["objects"][0]["rows"] == 1
        assert len(calls) == 1
        with pytest.raises(ValueError, match="archive_recipe_remote_hash_mismatch"):
            transport._archive_get(KEY, hashlib.sha256(DATA).hexdigest(), max_bytes=len(DATA) - 1)
        assert len(calls) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_workflow_scope_stays_manual_and_mutation_free():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    branch = text.split('if [[ -n "$FLOW_ETF_ARCHIVE_REFS_JSON" ]]; then')[1].split('exit 0')[0]
    assert 'test -z "$ARCHIVE_TARGET_DATE"' in branch
    assert 'test -z "$ARCHIVE_CURRENT_RUN_ID"' in branch
    assert 'test -z "$ARCHIVE_PREVIOUS_RUN_ID"' in branch
    assert "scripts/verify_etf_archive_partitions.py" in branch
    assert not any(word in branch for word in ("gh run", "curl", "wrangler", "claim", "publish"))
    assert "if: ${{ inputs.execution_mode == 'archive-verify' }}" in text
    assert "FLOW_ETF_ARCHIVE_REFS_JSON: ${{ inputs.request_json }}" in text
