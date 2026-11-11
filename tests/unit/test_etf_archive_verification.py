import gzip
import hashlib
import json
from copy import deepcopy

import pytest

from scripts.build_phase0_archive_recipe import ArchiveReadError
from scripts import verify_etf_archive_partitions as verifier
from scripts.verify_etf_archive_partitions import (
    CONTRACT, DATE_CANDIDATE_CONTRACT, ArchiveVerificationError, resolve_date_candidate, verify,
)


DATA = b"trade_date,market,instrument_code,shares\n2026-10-14,SZSE,159001,100\n"
KEY = "market-data/normalized/szse/etf_scale/2026-10-14/" + "a" * 64 + "/data.csv"


def raw_fixture():
    raw = b'{"official": "fixture"}'
    digest = hashlib.sha256(raw).hexdigest()
    prefix = f"market-data/raw/sse/etf_scale/2026-10-13/{digest}"
    value = {"contract": verifier.RAW_SOURCE_CONTRACT, "partition": {
        "sourceId": "sse", "tradeDate": "2026-10-13", "rawSha256": digest}}
    metadata = {"source": {"source_id": "sse", "dataset": "etf_scale"},
                "sha256": digest, "requested_date": "2026-10-13",
                "release_id": f"etf_scale:sse:2026-10-13:{digest[:16]}",
                "archived_raw_key": prefix + ".json"}
    return value, metadata, raw, prefix


def test_raw_source_exact_two_reads_and_no_recovery_claim():
    value, metadata, raw, prefix = raw_fixture()
    calls = []
    def read(key, digest, is_metadata):
        calls.append((key, digest, is_metadata))
        return json.dumps(metadata).encode() if is_metadata else raw
    receipt, stored_metadata, stored_raw = verifier.verify_raw_source(value, read=read)
    assert [c[0] for c in calls] == [prefix + ".metadata.json", prefix + ".json"]
    assert [c[2] for c in calls] == [True, False]
    assert receipt["archiveReadCount"] == 2
    assert json.loads(stored_metadata) == metadata and stored_raw == raw
    assert not receipt["provesPeriodRecovery"] and not any(receipt["sideEffects"].values())


@pytest.mark.parametrize("field", ["sha256", "requested_date", "release_id", "source", "archived_raw_key"])
def test_raw_metadata_conflict_stops_before_raw_get(field):
    value, metadata, raw, prefix = raw_fixture()
    metadata[field] = "invalid"
    calls = []
    def read(*args):
        calls.append(args)
        return json.dumps(metadata).encode()
    with pytest.raises(ArchiveVerificationError) as caught:
        verifier.verify_raw_source(value, read=read)
    assert len(calls) == 1
    assert caught.value.receipt["archiveReadAttempts"] == 1


def test_raw_hash_conflict_and_http_failure_never_retry():
    value, metadata, raw, prefix = raw_fixture()
    calls = []
    def read(key, digest, is_metadata):
        calls.append(key)
        return json.dumps(metadata).encode() if is_metadata else b"wrong"
    with pytest.raises(ArchiveVerificationError):
        verifier.verify_raw_source(value, read=read)
    assert len(calls) == 2
    def failed(*args):
        raise ArchiveReadError("archive_recipe_remote_http_error", status=404)
    with pytest.raises(ArchiveVerificationError) as caught:
        verifier.verify_raw_source(value, read=failed)
    assert caught.value.receipt["archiveReadAttempts"] == 1
    assert caught.value.receipt["archiveReadCount"] == 0
    assert caught.value.receipt["failure"]["httpStatus"] == 404


def test_raw_invalid_scope_rejected_without_reads():
    value, *_ = raw_fixture()
    value["partition"]["tradeDate"] = "2026-02-30"
    with pytest.raises(ArchiveVerificationError) as caught:
        verifier.verify_raw_source(value, read=lambda *_: pytest.fail("unexpected read"))
    assert caught.value.receipt["archiveReadAttempts"] == 0


def test_raw_cli_saves_verified_bytes_only(monkeypatch, tmp_path):
    value, metadata, raw, _ = raw_fixture()
    monkeypatch.setenv("FLOW_ETF_ARCHIVE_REQUEST_JSON", json.dumps(value))
    def read(key, digest, **kwargs):
        assert kwargs["max_bytes"] == verifier.MAX_SOURCE_BYTES
        return json.dumps(metadata).encode() if kwargs["metadata_raw"] else raw
    monkeypatch.setattr(verifier, "_archive_get", read)
    output = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["verifier", "--output", str(output)])
    verifier.main()
    assert json.loads(output.read_text())["status"] == "RAW_SOURCE_BYTES_VERIFIED"
    assert (tmp_path / "raw-source-evidence/source.bin").read_bytes() == raw
    assert json.loads((tmp_path / "raw-source-evidence/metadata.json").read_bytes()) == metadata


@pytest.mark.parametrize("metadata_oversize", [True, False])
def test_raw_size_limits_stop_without_retry(metadata_oversize):
    value, metadata, raw, _ = raw_fixture()
    calls = []
    def read(key, digest, is_metadata):
        calls.append(key)
        if is_metadata and not metadata_oversize:
            return json.dumps(metadata).encode()
        return b"x" * (verifier.MAX_SOURCE_BYTES + 1)
    with pytest.raises(ArchiveVerificationError):
        verifier.verify_raw_source(value, read=read)
    assert len(calls) == (1 if metadata_oversize else 2)


def test_raw_cli_subprocess_invalid_scope_is_offline(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    value, *_ = raw_fixture()
    value["partition"]["sourceId"] = "unsupported"
    env = {**os.environ, "PYTHONPATH": str(root / "src") + ":" + str(root),
           "FLOW_ETF_ARCHIVE_REQUEST_JSON": json.dumps(value)}
    for key in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        env.pop(key, None)
    output = tmp_path / "receipt.json"
    result = subprocess.run([sys.executable, str(root / "scripts/verify_etf_archive_partitions.py"),
                             "--output", str(output)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    receipt = json.loads(output.read_text())
    assert receipt["archiveReadAttempts"] == 0
    assert receipt["status"] == "RAW_SOURCE_VERIFICATION_FAILED"
    assert not (tmp_path / "raw-source-evidence").exists()


def request():
    return {"contract": CONTRACT, "objects": [{"objectKey": KEY, "sha256": hashlib.sha256(DATA).hexdigest()}]}


def candidate_request(expected=None):
    return {"contract": DATE_CANDIDATE_CONTRACT, "partition": {
        "sourceId": "sse", "tradeDate": "2026-10-13",
        "expectedSha256": expected or hashlib.sha256(DATA.replace(b"SZSE", b"SSE").replace(b"10-14", b"10-13")).hexdigest(),
    }}


def candidate_response(data=None, *, key=None, declared=None, count="1", selection="single", content_type="text/csv; charset=utf-8"):
    normalized = data or DATA.replace(b"SZSE", b"SSE").replace(b"10-14", b"10-13")
    return ({"key": key or "market-data/normalized/sse/etf_scale/2026-10-13/" + "b" * 64 + "/data.csv",
             "sha256": declared or hashlib.sha256(normalized).hexdigest(),
             "tradeDate": "2026-10-13", "candidateCount": count, "selection": selection,
             "contentType": content_type}, normalized)


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
    monkeypatch.setenv("FLOW_ETF_ARCHIVE_REQUEST_JSON", json.dumps(request()))
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


@pytest.mark.parametrize("mutation", ["contract", "extra", "source", "date", "hash"])
def test_date_candidate_scope_rejected_before_network(mutation):
    value = deepcopy(candidate_request())
    if mutation == "contract": value["contract"] = "other"
    if mutation == "extra": value["partition"]["extra"] = "value"
    if mutation == "source": value["partition"]["sourceId"] = "bse"
    if mutation == "date": value["partition"]["tradeDate"] = "2026-02-30"
    if mutation == "hash": value["partition"]["expectedSha256"] = "not-a-hash"
    with pytest.raises(ValueError):
        resolve_date_candidate(value, read=lambda: pytest.fail("network must not run"))


def test_date_candidate_resolution_reports_exact_bytes_and_hash_mismatch_honestly():
    headers, body = candidate_response()
    value = candidate_request(expected="0" * 64)
    result = resolve_date_candidate(value, read=lambda: (headers, body))
    candidate = result["candidate"]
    assert result["status"] == "DATE_CANDIDATE_RESOLVED"
    assert candidate["objectKey"] == headers["key"]
    assert candidate["actualSha256"] == hashlib.sha256(body).hexdigest()
    assert candidate["expectedSha256MatchesBytes"] is False
    assert result["provesExpectedBytes"] is False
    assert result["provesOfficialProvenance"] is False
    assert result["archiveReadCount"] == 1
    assert not any(result["sideEffects"].values())


def test_date_candidate_surfaces_stale_declared_hash_without_claiming_integrity():
    body = DATA.replace(b"SZSE", b"SSE").replace(b"10-14", b"10-13")
    headers, wire = candidate_response(body, declared="d" * 64)
    result = resolve_date_candidate(candidate_request(), read=lambda: (headers, wire))
    assert result["candidate"]["actualSha256"] == hashlib.sha256(body).hexdigest()
    assert result["candidate"]["declaredHashMatchesBytes"] is False
    assert result["candidate"]["expectedSha256MatchesBytes"] is True
    assert result["provesExpectedBytes"] is True
    assert result["provesOfficialProvenance"] is False


def test_date_candidate_resolution_accepts_bounded_gzip_and_detects_expansion_limit():
    normalized = DATA.replace(b"SZSE", b"SSE").replace(b"10-14", b"10-13")
    wire = gzip.compress(normalized, mtime=0)
    headers, _ = candidate_response(
        data=normalized,
        key="market-data/normalized/sse/etf_scale/20261013/" + "c" * 64 + "/data.csv.gz",
        content_type="application/gzip",
    )
    result = resolve_date_candidate(candidate_request(), read=lambda: (headers, wire))
    assert result["candidate"]["bytes"] == len(normalized)
    assert result["candidate"]["responseBytes"] == len(wire)
    assert result["candidate"]["expectedSha256MatchesBytes"] is True
    with pytest.raises(ValueError, match="expanded_size_exceeded"):
        verifier._bounded_gzip_decompress(gzip.compress(b"x" * 1000), max_bytes=32)


def test_date_candidate_resolution_rejects_wire_payload_over_two_megabytes():
    headers, body = candidate_response()
    oversized = body + b"x" * (verifier.MAX_SOURCE_BYTES + 1 - len(body))
    with pytest.raises(ArchiveVerificationError) as captured:
        resolve_date_candidate(candidate_request(), read=lambda: (headers, oversized))
    assert captured.value.receipt["failure"]["errorCode"] == "archive_candidate_validation_failed"
    assert captured.value.receipt["archiveReadCount"] == 0


def test_date_candidate_rejects_wrong_date_and_ambiguous_selection():
    headers, body = candidate_response(count="2", selection="single")
    with pytest.raises(ArchiveVerificationError) as captured:
        resolve_date_candidate(candidate_request(), read=lambda: (headers, body))
    assert captured.value.receipt["failure"]["errorCode"] == "archive_candidate_validation_failed"
    assert captured.value.receipt["archiveReadCount"] == 0
    headers, body = candidate_response()
    headers["tradeDate"] = "2026-10-12"
    with pytest.raises(ArchiveVerificationError):
        resolve_date_candidate(candidate_request(), read=lambda: (headers, body))


def test_date_candidate_http_failure_has_safe_bounded_receipt_and_no_retry():
    calls = []
    def read():
        calls.append(1)
        raise ArchiveReadError("archive_candidate_remote_http_error", status=404, ray="ray-date-123")
    with pytest.raises(ArchiveVerificationError) as captured:
        resolve_date_candidate(candidate_request(), read=read)
    assert len(calls) == 1
    assert captured.value.receipt["failure"] == {
        "index": 1, "sourceId": "sse", "tradeDate": "2026-10-13",
        "errorCode": "archive_candidate_remote_http_error", "httpStatus": 404, "rayId": "ray-date-123",
    }
    assert not any(captured.value.receipt["sideEffects"].values())


def test_date_candidate_cli_writes_failure_receipt_and_returns_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FLOW_ETF_ARCHIVE_REQUEST_JSON", json.dumps(candidate_request()))
    monkeypatch.setattr(verifier, "_archive_get_date_candidate", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(ArchiveReadError("archive_candidate_remote_http_error", status=409)))
    output = tmp_path / "candidate-receipt.json"
    monkeypatch.setattr("sys.argv", ["verify_etf_archive_partitions.py", "--output", str(output)])
    with pytest.raises(SystemExit) as captured:
        verifier.main()
    assert captured.value.code == 1
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "DATE_CANDIDATE_RESOLUTION_FAILED"
    assert receipt["failure"]["httpStatus"] == 409
    assert "409" in capsys.readouterr().out


def test_actual_date_candidate_get_uses_one_existing_admin_route(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit
    from scripts import build_phase0_archive_recipe as transport
    normalized = DATA.replace(b"SZSE", b"SSE").replace(b"10-14", b"10-13")
    headers, _ = candidate_response(data=normalized, count="2", selection="latest-uploaded")
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("authorization"), self.headers.get("cf-access-client-id")))
            self.send_response(200)
            self.send_header("x-flow-normalized-key", headers["key"])
            self.send_header("x-flow-normalized-sha256", headers["sha256"])
            self.send_header("x-flow-trade-date", headers["tradeDate"])
            self.send_header("x-flow-normalized-candidate-count", headers["candidateCount"])
            self.send_header("x-flow-normalized-selection", headers["selection"])
            self.send_header("content-type", headers["contentType"])
            self.end_headers()
            self.wfile.write(normalized)
        def log_message(self, *_): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(transport, "ARCHIVE_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, "fixture-only")
    try:
        result = resolve_date_candidate(candidate_request())
        path, auth, access_id = calls[0]
        query = parse_qs(urlsplit(path).query)
        assert len(calls) == 1
        assert urlsplit(path).path == "/api/v1/admin/history-artifact"
        assert query == {"source": ["sse"], "dataset": ["etf_scale"], "date": ["2026-10-13"], "file": ["data"]}
        assert auth == "Bearer fixture-only" and access_id == "fixture-only"
        assert result["candidate"]["selection"] == "latest-uploaded"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_workflow_scope_stays_manual_and_mutation_free():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/pipeline-executor-candidate.yml").read_text()
    branch = text.split('if [[ -n "$FLOW_ETF_ARCHIVE_REQUEST_JSON" ]]; then')[1].split('exit 0')[0]
    assert 'test -z "$ARCHIVE_TARGET_DATE"' in branch
    assert 'test -z "$ARCHIVE_CURRENT_RUN_ID"' in branch
    assert 'test -z "$ARCHIVE_PREVIOUS_RUN_ID"' in branch
    assert "scripts/verify_etf_archive_partitions.py" in branch
    assert not any(word in branch for word in ("gh run", "curl", "wrangler", "claim", "publish"))
    assert "if: ${{ inputs.execution_mode == 'archive-verify' }}" in text
    assert "FLOW_ETF_ARCHIVE_REQUEST_JSON: ${{ inputs.request_json }}" in text
    assert "flow-etf-date-candidate-resolution-v1" in (Path(__file__).resolve().parents[2] / "scripts/verify_etf_archive_partitions.py").read_text()
