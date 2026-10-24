"""A collector bundle can supply exact archive refs without a manual key list."""
import base64
import gzip
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trading_desk.phase0_archive import validate_recipe
from trading_desk.phase0_archive_recipe import (
    adjacent_object_from_bundle, build_archive_recipe, official_calendar_from_bundle,
)
from test_phase0_archive import DATES, fixture_recipe
from scripts import build_phase0_archive_recipe as cli


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _inputs():
    fixture, stored = fixture_recipe()
    manifest = json.loads(gzip.decompress(base64.b64decode(fixture["manifestGzipBase64"])))
    history = []
    for item in manifest["artifacts"]:
        source, dataset, day, sha = (item["source"]["source_id"], item["source"]["dataset"],
                                     item["requested_date"], item["sha256"])
        extension = item["raw_path"].rsplit(".", 1)[-1]
        prefix = f"market-data/raw/{source}/{dataset}/{day}/{sha}"
        normalized = []
        for path in item["normalized_path"].split(";"):
            ref = next(ref for ref in fixture["files"] if ref["path"] == path)
            content = stored[ref["objectKey"]]
            normalized.append({"name": ref["objectKey"].rsplit("/", 1)[-1].split(".")[0],
                               "sha256": _sha(content), "contentBase64": base64.b64encode(content).decode()})
        history.append({"releaseId": f"{dataset}:{source}:{day}:{sha[:16]}",
                        "sourceId": source, "dataset": dataset, "requestedDate": day, "sha256": sha,
                        "extension": extension, "rawContentBase64": base64.b64encode(stored[f"{prefix}.{extension}"]).decode(),
                        "metadata": {"sha256": sha, "source": item["source"], "requested_date": day,
                            "observed_at": item["observed_at"], "status_code": item["status_code"],
                            "bytes": len(stored[f"{prefix}.{extension}"])}, "normalized": normalized})
    return manifest, {"version": 1, "history": {"runId": "fixture", "asOf": DATES[-1], "artifacts": history}}, fixture["calendar"]


def _official_inputs():
    manifest, bundle, _ = _inputs()
    rows = [{"opDate": day.replace("-", "")} for day in reversed(DATES)]
    response = {"beginDate": DATES[0].replace("-", ""), "endDate": DATES[-1].replace("-", ""),
                "result": rows, "pageHelp": {"pageCount": 1, "total": len(rows)}}
    raw = json.dumps(response, separators=(",", ":")).encode()
    source = next(part for part in bundle["history"]["artifacts"]
                  if part["sourceId"] == "sse" and part["dataset"] == "margin_market_daily")
    source["rawContentBase64"] = base64.b64encode(raw).decode()
    source["sha256"] = source["metadata"]["sha256"] = _sha(raw)
    source["metadata"]["bytes"] = len(raw)
    item = next(item for item in manifest["artifacts"]
                if item["source"]["source_id"] == "sse" and item["source"]["dataset"] == "margin_market_daily")
    item["sha256"] = _sha(raw)
    return manifest, bundle


def _previous_adjacent(manifest, bundle):
    source = next(part for part in bundle["history"]["artifacts"]
                  if part["sourceId"] == "szse" and part["dataset"] == "etf_scale")
    prior = json.loads(json.dumps(source))
    prior["requestedDate"] = DATES[-2]
    wire = base64.b64decode(prior["normalized"][0]["contentBase64"])
    revision = _sha(f"{prior['sha256']}:{_sha(wire)}".encode())
    key = f"market-data/normalized/szse/etf_scale/{DATES[-2]}/{revision}/data.csv"
    manifest["archivedEtfInputs"] = [{"sourceId": "szse", "tradeDate": DATES[-2], "objectKey": key,
        "sha256": _sha(wire), "revisionId": revision,
        "normalizedPath": "reports/phase0/adjacent-szse.csv"}]
    previous = {"version": 1, "history": {"runId": "prior", "asOf": DATES[-2], "artifacts": [prior]}}
    return previous, key, wire


def test_recipe_from_bundle_has_exact_hashes_and_is_deterministic():
    manifest, bundle, calendar = _inputs()
    recipe, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    assert validate_recipe(recipe, DATES[-1]) == manifest
    assert len(recipe["files"]) == len(objects)
    assert all(ref["sha256"] == _sha(objects[ref["objectKey"]]) and ref["bytes"] == len(objects[ref["objectKey"]])
               for ref in recipe["files"])
    scale = next(ref for ref in recipe["files"] if ref["objectKey"].startswith("market-data/normalized/sse/etf_scale/"))
    assert scale["objectKey"].split("/")[-2] != bundle["history"]["artifacts"][0]["sha256"]
    repeated, _ = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    assert repeated == recipe


@pytest.mark.parametrize("tamper", ["identity", "raw", "normalized", "missing", "calendar", "gzip_bomb"])
def test_recipe_rejects_inconsistent_or_incomplete_bundle(tamper):
    manifest, bundle, calendar = _inputs()
    if tamper == "identity": bundle["history"]["artifacts"][0]["requestedDate"] = "2026-07-13"
    if tamper == "raw": bundle["history"]["artifacts"][0]["rawContentBase64"] = base64.b64encode(b"wrong").decode()
    if tamper == "normalized": bundle["history"]["artifacts"][0]["normalized"][0]["sha256"] = "0" * 64
    if tamper == "missing": bundle["history"]["artifacts"].pop()
    if tamper == "calendar": calendar["datesSha256"] = "0" * 64
    if tamper == "gzip_bomb":
        part = bundle["history"]["artifacts"][0]["normalized"][0]
        part["contentEncoding"] = "gzip"
        part["contentBase64"] = base64.b64encode(gzip.compress(b"x" * 2_000_001)).decode()
    with pytest.raises(ValueError):
        build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])


def test_adjacent_object_must_be_supplied_with_exact_hash():
    manifest, bundle, calendar = _inputs()
    content = b"instrument_code,shares\n159915,100\n"
    revision = "a" * 64
    key = f"market-data/normalized/szse/etf_scale/{DATES[-2]}/{revision}/data.csv"
    manifest["archivedEtfInputs"] = [{"sourceId": "szse", "tradeDate": DATES[-2], "objectKey": key,
        "sha256": _sha(content), "revisionId": revision, "normalizedPath": "data/normalized/adjacent-szse.csv"}]
    with pytest.raises(ValueError, match="archive_recipe_adjacent_object_required"):
        build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    with pytest.raises(ValueError, match="archive_recipe_adjacent_hash_mismatch"):
        build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1],
                             adjacent_objects={key: b"wrong"})
    recipe, _ = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1],
                                     adjacent_objects={key: content})
    assert validate_recipe(recipe, DATES[-1]) == manifest


def test_cli_live_verification_is_exact_bounded_read_only(tmp_path, monkeypatch):
    manifest, bundle, calendar = _inputs()
    _, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    paths = [tmp_path / name for name in ("manifest.json", "bundle.json", "calendar.json")]
    for path, value in zip(paths, (manifest, bundle, calendar), strict=True):
        path.write_text(json.dumps(value))
    output = tmp_path / "candidate.json"
    reads = []
    def exact_get(key, digest):
        reads.append(key)
        assert _sha(objects[key]) == digest
        return objects[key]
    monkeypatch.setattr(cli, "_archive_get", exact_get)
    def metadata_get(key, digest):
        reads.append(key)
        assert json.loads(objects[key])["sha256"] == digest
        return objects[key]
    monkeypatch.setattr(cli, "_archive_metadata_get", metadata_get)
    monkeypatch.setattr(sys, "argv", ["archive-recipe", "--manifest", str(paths[0]),
        "--history-bundle", str(paths[1]), "--official-calendar", str(paths[2]),
        "--target-date", DATES[-1], "--verify-live-archive", "--output", str(output)])
    assert cli.main() == 0
    candidate = json.loads(output.read_text())
    assert candidate["status"] == "ARCHIVE_OBJECTS_VERIFIED"
    assert candidate["archiveReadCount"] == len(reads) == len(objects)
    assert len(set(reads)) == len(reads)
    output.unlink()
    monkeypatch.setattr(cli, "_archive_get", lambda key, digest: b"wrong")
    with pytest.raises(ValueError, match="archive_recipe_remote_content_mismatch"):
        cli.main()
    failure = json.loads(output.read_text())
    assert failure["status"] == "ARCHIVE_VERIFICATION_FAILED"
    assert failure["errorCode"] == "archive_recipe_remote_content_mismatch"
    assert "recipe" not in failure


def test_official_calendar_and_prior_adjacent_close_real_collector_shape(tmp_path, monkeypatch):
    manifest, bundle = _official_inputs()
    previous, key, wire = _previous_adjacent(manifest, bundle)
    calendar = official_calendar_from_bundle(manifest=manifest, history_bundle=bundle)
    assert calendar["dates"] == DATES
    assert adjacent_object_from_bundle(manifest=manifest, previous_bundle=previous) == {key: wire}
    recipe, _ = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar,
                                     target=DATES[-1], adjacent_objects={key: wire})
    assert validate_recipe(recipe, DATES[-1]) == manifest
    inputs = [tmp_path / name for name in ("manifest.json", "bundle.json", "previous.json")]
    for path, value in zip(inputs, (manifest, bundle, previous), strict=True):
        path.write_text(json.dumps(value))
    monkeypatch.setattr(cli, "_archive_get", lambda *_: pytest.fail("offline construction fetched R2"))
    output = tmp_path / "candidate.json"
    monkeypatch.setattr(sys, "argv", ["archive-recipe", "--manifest", str(inputs[0]),
        "--history-bundle", str(inputs[1]), "--previous-history-bundle", str(inputs[2]),
        "--target-date", DATES[-1], "--output", str(output)])
    assert cli.main() == 0
    candidate = json.loads(output.read_text())
    assert candidate["status"] == "UNVERIFIED_CANDIDATE" and candidate["archiveReadCount"] == 0
    assert candidate["recipe"] == recipe


def test_live_verification_reads_every_object_even_when_adjacent_bundle_is_supplied(tmp_path, monkeypatch):
    manifest, bundle = _official_inputs()
    previous, key, wire = _previous_adjacent(manifest, bundle)
    calendar = official_calendar_from_bundle(manifest=manifest, history_bundle=bundle)
    recipe, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar,
                                           target=DATES[-1], adjacent_objects={key: wire})
    first_metadata = next(k for k in objects if k.endswith(".metadata.json"))
    original_metadata = json.loads(objects[first_metadata])
    original_metadata["observed_at"] = "2026-07-16T01:00:00Z"
    objects[first_metadata] = json.dumps(original_metadata).encode()
    inputs = [tmp_path / name for name in ("manifest.json", "bundle.json", "previous.json")]
    for path, value in zip(inputs, (manifest, bundle, previous), strict=True):
        path.write_text(json.dumps(value))
    calls = []
    def exact_read(object_key, digest):
        calls.append(object_key)
        content = objects[object_key]
        assert _sha(content) == digest
        return content
    monkeypatch.setattr(cli, "_archive_get", exact_read)
    def metadata_read(object_key, digest):
        calls.append(object_key)
        assert json.loads(objects[object_key])["sha256"] == digest
        return objects[object_key]
    monkeypatch.setattr(cli, "_archive_metadata_get", metadata_read)
    output = tmp_path / "verified.json"
    monkeypatch.setattr(sys, "argv", ["archive-recipe", "--manifest", str(inputs[0]),
        "--history-bundle", str(inputs[1]), "--previous-history-bundle", str(inputs[2]),
        "--target-date", DATES[-1], "--verify-live-archive", "--output", str(output)])
    assert cli.main() == 0
    result = json.loads(output.read_text())
    assert result["status"] == "ARCHIVE_OBJECTS_VERIFIED"
    assert result["archiveReadCount"] == len(recipe["files"])
    assert sorted(calls) == sorted(objects)
    assert result["archiveReadAttempts"] == len(objects)
    assert next(ref for ref in result["recipe"]["files"] if ref["objectKey"] == first_metadata)["sha256"] == _sha(objects[first_metadata])


@pytest.mark.parametrize("tamper", ["calendar_raw_hash", "calendar_date", "calendar_page",
                                    "adjacent_raw_hash", "adjacent_wire", "adjacent_revision", "adjacent_path",
                                    "adjacent_prior_date"])
def test_two_bundle_proof_rejects_missing_or_modified_evidence(tamper):
    manifest, bundle = _official_inputs()
    previous, _, _ = _previous_adjacent(manifest, bundle)
    official = next(part for part in bundle["history"]["artifacts"]
                    if part["sourceId"] == "sse" and part["dataset"] == "margin_market_daily")
    if tamper == "calendar_raw_hash": official["sha256"] = "0" * 64
    if tamper in {"calendar_date", "calendar_page"}:
        raw = json.loads(base64.b64decode(official["rawContentBase64"]))
        if tamper == "calendar_date": raw["result"].pop()
        else: raw["pageHelp"]["pageCount"] = 2
        encoded = json.dumps(raw, separators=(",", ":")).encode()
        official["rawContentBase64"] = base64.b64encode(encoded).decode()
        official["sha256"] = _sha(encoded)
        manifest_item = next(item for item in manifest["artifacts"]
                             if item["source"]["source_id"] == "sse" and item["source"]["dataset"] == "margin_market_daily")
        manifest_item["sha256"] = _sha(encoded)
    if tamper == "adjacent_raw_hash": previous["history"]["artifacts"][0]["sha256"] = "0" * 64
    if tamper == "adjacent_wire": previous["history"]["artifacts"][0]["normalized"][0]["contentBase64"] = base64.b64encode(b"wrong").decode()
    if tamper == "adjacent_revision": manifest["archivedEtfInputs"][0]["revisionId"] = "0" * 64
    if tamper == "adjacent_path": manifest["archivedEtfInputs"][0]["normalizedPath"] = "reports/phase0/other.csv"
    if tamper == "adjacent_prior_date": previous["history"]["asOf"] = DATES[-1]
    with pytest.raises(ValueError):
        calendar = official_calendar_from_bundle(manifest=manifest, history_bundle=bundle)
        adjacent = adjacent_object_from_bundle(manifest=manifest, previous_bundle=previous)
        build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar,
                             target=DATES[-1], adjacent_objects=adjacent)


@pytest.mark.parametrize("status", [200, 302, 403])
@pytest.mark.parametrize("metadata_raw", [False, True])
def test_archive_http_client_identifies_itself_and_never_follows_redirects(monkeypatch, status, metadata_raw):
    requests = []
    content = b"official archive bytes"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({"path": self.path, "headers": dict(self.headers)})
            self.send_response(status)
            self.send_header("x-flow-archive-key", "exact-key")
            self.send_header("x-flow-archive-sha256", _sha(content))
            self.send_header("x-flow-archive-raw-sha256", "a" * 64)
            self.send_header("cf-ray", "abc123-DFW")
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(cli, "ARCHIVE_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    for name in ("FLOW_INGEST_API_KEY", "FLOW_CF_ACCESS_CLIENT_ID", "FLOW_CF_ACCESS_CLIENT_SECRET"):
        monkeypatch.setenv(name, f"fixture-{name}")
    try:
        digest = "a" * 64 if metadata_raw else _sha(content)
        if status == 200:
            assert cli._archive_get("exact-key", digest, metadata_raw=metadata_raw) == content
        else:
            with pytest.raises(cli.ArchiveReadError) as raised:
                cli._archive_get("exact-key", digest, metadata_raw=metadata_raw)
            assert raised.value.status == status and raised.value.ray == "abc123-DFW"
            assert str(raised.value) == "archive_recipe_remote_http_error"
        assert len(requests) == 1
        assert ("archive_metadata_raw_sha256=" in requests[0]["path"]) == metadata_raw
        headers = {key.lower(): value for key, value in requests[0]["headers"].items()}
        assert headers["user-agent"] == cli.ARCHIVE_USER_AGENT
        assert headers["accept"] == "application/octet-stream"
        assert headers["authorization"] == "Bearer fixture-FLOW_INGEST_API_KEY"
        assert headers["cf-access-client-id"] == "fixture-FLOW_CF_ACCESS_CLIENT_ID"
        assert headers["cf-access-client-secret"] == "fixture-FLOW_CF_ACCESS_CLIENT_SECRET"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_cli_preserves_safe_failure_evidence_and_stops_at_first_http_error(tmp_path, monkeypatch):
    manifest, bundle, calendar = _inputs()
    paths = [tmp_path / name for name in ("manifest.json", "bundle.json", "calendar.json")]
    for path, value in zip(paths, (manifest, bundle, calendar), strict=True):
        path.write_text(json.dumps(value))
    output = tmp_path / "result.json"
    calls = []
    def denied(key, digest):
        calls.append((key, digest))
        raise cli.ArchiveReadError("archive_recipe_remote_http_error", status=403, ray="abc123-DFW")
    monkeypatch.setattr(cli, "_archive_get", denied)
    monkeypatch.setattr(cli, "_archive_metadata_get", denied)
    monkeypatch.setattr(sys, "argv", ["archive-recipe", "--manifest", str(paths[0]),
        "--history-bundle", str(paths[1]), "--official-calendar", str(paths[2]),
        "--target-date", DATES[-1], "--verify-live-archive", "--output", str(output)])
    with pytest.raises(cli.ArchiveReadError):
        cli.main()
    failure = json.loads(output.read_text())
    assert failure == {"status": "ARCHIVE_VERIFICATION_FAILED", "targetDate": DATES[-1],
        "archiveReadAttempts": 1, "archiveReadCount": 0, "failedObjectKey": calls[0][0],
        "errorCode": "archive_recipe_remote_http_error", "httpStatus": 403, "cfRay": "abc123-DFW"}
    assert len(calls) == 1 and "recipe" not in failure


def test_recipe_binds_actual_first_observation_metadata_without_changing_official_bytes():
    manifest, bundle, calendar = _inputs()
    candidate, original = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    stored = {ref["objectKey"]: original[ref["objectKey"]] for ref in candidate["files"]
              if ref["objectKey"].endswith(".metadata.json")}
    key = next(iter(stored))
    earlier = json.loads(stored[key])
    earlier["observed_at"] = "2026-07-16T01:00:00Z"
    stored[key] = json.dumps(earlier, ensure_ascii=False).encode()
    exact, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar,
        target=DATES[-1], metadata_objects=stored)
    assert objects[key] == stored[key] != original[key]
    assert next(ref for ref in exact["files"] if ref["objectKey"] == key)["sha256"] == _sha(stored[key])
    assert all(objects[k] == value for k, value in original.items() if k not in stored)
    assert validate_recipe(exact, DATES[-1]) == manifest


@pytest.mark.parametrize("fault", ["missing", "raw_sha", "date", "source", "raw_key", "bytes", "malformed", "extra"])
def test_actual_metadata_must_match_exact_raw_identity(fault):
    manifest, bundle, calendar = _inputs()
    recipe, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    stored = {ref["objectKey"]: objects[ref["objectKey"]] for ref in recipe["files"]
              if ref["objectKey"].endswith(".metadata.json")}
    key = next(iter(stored))
    value = json.loads(stored[key])
    if fault == "raw_sha": value["sha256"] = "0" * 64
    if fault == "date": value["requested_date"] = "2026-07-01"
    if fault == "source": value["source"]["source_id"] = "szse"
    if fault == "raw_key": value["archived_raw_key"] += "/other"
    if fault == "bytes": value["bytes"] += 1
    stored[key] = json.dumps(value).encode()
    if fault == "missing": del stored[key]
    if fault == "malformed": stored[key] = b"not JSON"
    if fault == "extra": stored["other"] = b"{}"
    with pytest.raises(ValueError, match="archive_recipe_stored_metadata"):
        build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar,
            target=DATES[-1], metadata_objects=stored)
