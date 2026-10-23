"""A collector bundle can supply exact archive refs without a manual key list."""
import base64
import gzip
import hashlib
import json
import sys

import pytest

from trading_desk.phase0_archive import validate_recipe
from trading_desk.phase0_archive_recipe import build_archive_recipe
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
                        "metadata": {"sha256": sha}, "normalized": normalized})
    return manifest, {"version": 1, "history": {"runId": "fixture", "asOf": DATES[-1], "artifacts": history}}, fixture["calendar"]


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
    assert not output.exists()
