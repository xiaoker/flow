from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_desk.adapters.official import OfficialMarketAdapters
from trading_desk.release_qa import validate_raw_to_normalized_mapping


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
