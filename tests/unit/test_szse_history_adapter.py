from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.adapters.official import OfficialMarketAdapters
from trading_desk.release_qa import validate_artifact_lineage, validate_raw_to_normalized_mapping


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_history_preserves_unrecorded_http_status_and_lineage(tmp_path, explicit_null):
    root = _archive(tmp_path)
    metadata_path = root / "2026-08-28/raw.metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("status_code")
    if explicit_null:
        metadata["status_code"] = None
    metadata_path.write_text(json.dumps(metadata))
    manifest_path = root / "manifest.json"
    original = json.loads(manifest_path.read_text())
    original["artifacts"][0].pop("status_code")
    manifest_path.write_text(json.dumps(original))
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    adapter = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")
    frame, artifact = adapter.fetch_szse_etf_history(root, "2026-08-28")
    assert artifact.status_code is None
    assert "historical_http_status_unrecorded" in artifact.warnings
    assert artifact.row_count == len(frame) == 1
    assert all(path.read_bytes() == wire for path, wire in before.items())
    manifest = {"artifacts": [artifact.to_dict()]}
    findings = validate_artifact_lineage(manifest, tmp_path) + validate_raw_to_normalized_mapping(manifest, tmp_path)
    # One source cannot satisfy the unchanged full-batch publisher gate.
    assert {f.check_id for f in findings if f.status == "fail"} == {"provenance.required_publishers"}
    assert next(f for f in findings if f.check_id == "lineage.metadata_matches_manifest").status == "pass"
    assert next(f for f in findings if f.check_id == "mapping.raw_to_normalized_values").status == "pass"


@pytest.mark.parametrize("status", [0, 201, 403, 429, "200", True])
def test_history_rejects_invalid_recorded_http_status(tmp_path, status):
    root = _archive(tmp_path)
    path = root / "2026-08-28/raw.metadata.json"
    metadata = json.loads(path.read_text())
    metadata["status_code"] = status
    path.write_text(json.dumps(metadata))
    adapter = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")
    with pytest.raises(ValueError, match="historical HTTP status"):
        adapter.fetch_szse_etf_history(root, "2026-08-28")


def test_history_cannot_claim_success_when_metadata_status_is_unrecorded(tmp_path):
    root = _archive(tmp_path)
    path = root / "2026-08-28/raw.metadata.json"
    metadata = json.loads(path.read_text())
    metadata.pop("status_code")
    path.write_text(json.dumps(metadata))
    # The manifest still claims 200; absent metadata cannot prove it.
    adapter = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")
    with pytest.raises(ValueError, match="historical HTTP status does not match"):
        adapter.fetch_szse_etf_history(root, "2026-08-28")


@pytest.mark.parametrize("status", [200.0, "200", True, 403, None])
def test_history_rejects_invalid_or_disagreeing_manifest_status(tmp_path, status):
    root = _archive(tmp_path)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"][0]["status_code"] = status
    path.write_text(json.dumps(manifest))
    adapter = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")
    with pytest.raises(ValueError, match="historical HTTP status"):
        adapter.fetch_szse_etf_history(root, "2026-08-28")


@pytest.mark.parametrize("length", ["absent", None, -1, 1, True,
    "nonhistorical", "wrong_url", "source_disagreement"])
def test_legacy_composite_recipe_preserves_metadata_and_proves_raw_length(length):
    from test_phase0_archive_recipe import _inputs, DATES
    from trading_desk.phase0_archive_recipe import build_archive_recipe

    manifest, bundle, calendar = _inputs()
    item = next(a for a in manifest["artifacts"] if a["source"]["source_id"] == "szse" and a["source"]["dataset"] == "etf_scale")
    source = item["source"]
    source["url"] = "https://www.szse.cn/api/report/ShowReport/data"
    source["date_semantics"] = "source_historical_scale_size_date"
    if length == "nonhistorical":
        source["date_semantics"] = "source_published"
    if length == "wrong_url":
        source["url"] = "https://www.szse.cn/api/report/other"
    item["status_code"] = None
    artifact = next(a for a in bundle["history"]["artifacts"] if a["sourceId"] == "szse" and a["dataset"] == "etf_scale")
    artifact["metadata"]["source"] = dict(source)
    artifact["metadata"].pop("status_code")
    artifact["metadata"].pop("bytes")
    recipe, objects = build_archive_recipe(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1])
    stored = {ref["objectKey"]: objects[ref["objectKey"]] for ref in recipe["files"] if ref["objectKey"].endswith(".metadata.json")}
    key = next(k for k in stored if "/raw/szse/etf_scale/" in k)
    metadata = json.loads(stored[key])
    if length == "source_disagreement":
        metadata["source"]["publisher"] = "different source"
        stored[key] = json.dumps(metadata).encode()
    elif length not in ("absent", "nonhistorical", "wrong_url"):
        metadata["bytes"] = length
        stored[key] = json.dumps(metadata).encode()
    kwargs = dict(manifest=manifest, history_bundle=bundle, calendar=calendar, target=DATES[-1], metadata_objects=stored)
    if length != "absent":
        with pytest.raises(ValueError, match="archive_recipe_stored_metadata_invalid"):
            build_archive_recipe(**kwargs)
    else:
        exact, bound = build_archive_recipe(**kwargs)
        assert bound[key] == stored[key]
        assert "bytes" not in json.loads(bound[key])
        assert "status_code" not in json.loads(bound[key])
        raw_ref = next(r for r in exact["files"] if r["path"] == item["raw_path"])
        assert raw_ref["bytes"] == len(bound[raw_ref["objectKey"]])


def _archive(tmp_path: Path, *, raw_digest: str | None = None) -> Path:
    root = tmp_path / "archive"
    day = root / "2026-08-28"
    day.mkdir(parents=True)
    raw = {
        "source": "深圳证券交易所",
        "dataset": "etf_scale",
        "requestedDate": "2026-08-28",
        "responses": [
            {
                "request": {"code": "159001", "start": "2026-08-28", "end": "2026-08-28"},
                "response": [{"data": [{"size_date": "2026-08-28", "current_size": "1234.5", "security_short_name": "货币ETF易方达"}]}],
            }
        ],
    }
    raw_path = day / "raw.json"
    raw_content = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw_path.write_bytes(raw_content)
    digest = hashlib.sha256(raw_content).hexdigest()
    metadata_path = day / "raw.metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": {
                    "source_id": "szse",
                    "publisher": "深圳证券交易所",
                    "dataset": "etf_scale",
                    "url": "https://fund.szse.cn/api/report/ShowReport/data",
                    "source_grade": "A",
                    "date_semantics": "source_historical_scale_size_date",
                },
                "source_id": "szse",
                "publisher": "深圳证券交易所",
                "dataset": "etf_scale",
                "source_grade": "A",
                "requested_date": "2026-08-28",
                "sha256": digest,
                "status_code": 200,
                "observed_at": "2026-08-29T01:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    normalized_path = day / "data.csv"
    pd.DataFrame(
        [
            {
                "trade_date": "2026-08-28",
                "market": "SZSE",
                "instrument_code": "159001",
                "instrument_name": "货币ETF易方达",
                "etf_type": "ETF",
                "investment_type": "货币市场基金",
                "shares": 12_345_000,
                "nav": "",
                "date_basis": "source_historical_scale_size_date",
            }
        ]
    ).to_csv(normalized_path, index=False)
    manifest = {
        "version": 1,
        "artifacts": [
            {
                "source": {
                    "source_id": "szse",
                    "publisher": "深圳证券交易所",
                    "dataset": "etf_scale",
                    "url": "https://fund.szse.cn/api/report/ShowReport/data",
                    "source_grade": "A",
                    "date_semantics": "source_historical_scale_size_date",
                },
                "requested_date": "2026-08-28",
                "observed_at": "2026-08-29T01:00:00Z",
                "status_code": 200,
                "content_type": "application/json",
                "raw_path": str(raw_path.relative_to(root)),
                "metadata_path": str(metadata_path.relative_to(root)),
                "normalized_path": str(normalized_path.relative_to(root)),
                "sha256": raw_digest or digest,
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_szse_history_adapter_accepts_verified_exact_date_archive(tmp_path: Path) -> None:
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    frame, artifact = adapters.fetch_szse_etf_history(_archive(tmp_path), "2026-08-28")

    assert frame.loc[0, "trade_date"] == "2026-08-28"
    assert frame.loc[0, "market"] == "SZSE"
    assert frame.loc[0, "instrument_code"] == "159001"
    assert frame.loc[0, "shares"] == 12_345_000
    assert frame.loc[0, "source_id"] == "szse"
    assert artifact.source.date_semantics == "source_historical_scale_size_date"
    assert artifact.row_count == 1


def test_szse_history_adapter_preserves_official_www_source_url(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    url = "https://www.szse.cn/api/report/ShowReport/data"
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["source"]["url"] = url
    manifest_path.write_text(json.dumps(manifest))
    metadata_path = root / "2026-08-28/raw.metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["source"]["url"] = url
    metadata["source_url"] = url
    metadata_path.write_text(json.dumps(metadata))
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    frame, artifact = adapters.fetch_szse_etf_history(root, "2026-08-28")

    assert len(frame) == 1
    assert artifact.source.url == url
    assert artifact.source.date_semantics == "source_historical_scale_size_date"


@pytest.mark.parametrize("url", [
    "http://www.szse.cn/api/report/ShowReport/data",
    "https://www.szse.cn.evil.example/api/report/ShowReport/data",
    "https://www.szse.cn@evil.example/api/report/ShowReport/data",
    "https://www.szse.cn/api/report/ShowReport/data?other=1",
    "https://www.szse.cn/api/report/ShowReport",
])
def test_szse_history_adapter_rejects_nonexact_historical_url(tmp_path: Path, url: str) -> None:
    root = _archive(tmp_path)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"][0]["source"]["url"] = url
    path.write_text(json.dumps(manifest))
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not historical-scale evidence"):
        adapters.fetch_szse_etf_history(root, "2026-08-28")


def test_szse_history_adapter_rejects_disagreeing_official_hosts(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"][0]["source"]["url"] = "https://www.szse.cn/api/report/ShowReport/data"
    path.write_text(json.dumps(manifest))
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metadata does not match"):
        adapters.fetch_szse_etf_history(root, "2026-08-28")


def test_szse_history_adapter_rejects_conflicting_metadata_source_url(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    path = root / "2026-08-28/raw.metadata.json"
    metadata = json.loads(path.read_text())
    metadata["source_url"] = "https://www.szse.cn/api/report/ShowReport/data"
    path.write_text(json.dumps(metadata))
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="metadata does not match"):
        adapters.fetch_szse_etf_history(root, "2026-08-28")


@pytest.mark.parametrize("target", ["2026-08-27", "2026-08-29"])
def test_szse_history_adapter_rejects_missing_archive_date(tmp_path: Path, target: str) -> None:
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exactly one SZSE ETF artifact"):
        adapters.fetch_szse_etf_history(_archive(tmp_path), target)


def test_szse_history_adapter_rejects_raw_hash_mismatch(tmp_path: Path) -> None:
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="raw SHA-256"):
        adapters.fetch_szse_etf_history(_archive(tmp_path, raw_digest="0" * 64), "2026-08-28")


def test_szse_history_adapter_rejects_archive_path_escape(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"][0]["raw_path"] = "../outside.json"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="escapes archive root"):
        adapters.fetch_szse_etf_history(root, "2026-08-28")


def test_szse_history_adapter_rejects_wrong_date_basis(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    normalized_path = root / "2026-08-28" / "data.csv"
    frame = pd.read_csv(normalized_path)
    frame.loc[0, "date_basis"] = "source_page_previous_trading_day"
    frame.to_csv(normalized_path, index=False)
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path / "data", run_id="run")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="date basis"):
        adapters.fetch_szse_etf_history(root, "2026-08-28")


def test_release_qa_rebuilds_historical_szse_rows_from_raw_envelope(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    findings = validate_raw_to_normalized_mapping(manifest, root)

    mapping = [item for item in findings if item.check_id == "mapping.raw_to_normalized_values"]
    assert len(mapping) == 1
    assert mapping[0].status == "pass"
