from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from trading_desk.adapters.official import (
    OfficialMarketAdapters,
    SSE_ETF,
    SSE_ETF_PCF,
    SZSE_ETF,
)
from trading_desk.models import FetchArtifact
from trading_desk.models import QualityFinding
from trading_desk.release_qa import (
    _future_only_failure,
    _expected_szse_etf,
    _pending_future_warning,
    validate_artifact_lineage,
    validate_combined_data,
    validate_etf_terminal_data,
    validate_etf_terminal_identity,
    validate_etf_cross_artifact_reconciliation,
    validate_raw_to_normalized_mapping,
)


def test_expected_szse_etf_matches_collector_universe_rule(tmp_path: Path) -> None:
    workbook = tmp_path / "szse.xlsx"
    pd.DataFrame(
        [
            {"基金代码": "159001", "基金简称": "示例ETF", "基金类别": "ETF", "当前规模(份)": "100", "净值": "1.0"},
            {"基金代码": "180101", "基金简称": "示例REIT", "基金类别": "REIT", "当前规模(份)": "200", "净值": "1.0"},
        ]
    ).to_excel(workbook, index=False)

    expected = _expected_szse_etf(workbook.read_bytes(), "2026-08-07")

    assert expected["instrument_code"].tolist() == ["159001"]
    assert len(expected) == 1


def _artifact(
    *, source, raw_path: Path, metadata_path: Path, normalized_path: Path, digest: str
) -> dict:
    return {
        "source": {
            "source_id": source.source_id,
            "publisher": source.publisher,
            "dataset": source.dataset,
            "url": source.url,
            "source_grade": source.source_grade,
            "date_semantics": source.date_semantics,
        },
        "requested_date": "2026-07-15" if source.source_id == "sse" else None,
        "observed_at": "2026-07-16T00:10:00+08:00",
        "status_code": 200,
        "content_type": "application/json",
        "raw_path": str(raw_path),
        "metadata_path": str(metadata_path),
        "normalized_path": str(normalized_path),
        "sha256": digest,
        "row_count": 1,
    }


def test_release_qa_detects_cross_source_normalized_path_collision(tmp_path: Path) -> None:
    shared = tmp_path / "shared.csv"
    pd.DataFrame([{"market": "SZSE"}]).to_csv(shared, index=False)
    artifacts = []
    for source in (SSE_ETF, SZSE_ETF):
        raw = tmp_path / f"{source.source_id}.raw"
        raw.write_bytes(source.source_id.encode())
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        metadata = tmp_path / f"{source.source_id}.metadata.json"
        payload = {
            "source": {"source_id": source.source_id, "dataset": source.dataset},
            "requested_date": "2026-07-15" if source.source_id == "sse" else None,
            "status_code": 200,
            "sha256": digest,
        }
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        artifacts.append(
            _artifact(
                source=source,
                raw_path=raw,
                metadata_path=metadata,
                normalized_path=shared,
                digest=digest,
            )
        )

    findings = validate_artifact_lineage({"artifacts": artifacts}, Path("/"))
    collision = next(
        finding for finding in findings if finding.check_id == "lineage.normalized_paths_unique"
    )

    assert collision.status == "fail"
    assert str(shared) in collision.evidence["collisions"]


def test_release_qa_detects_tampered_raw_response(tmp_path: Path) -> None:
    raw = tmp_path / "source.json"
    raw.write_text("changed", encoding="utf-8")
    normalized = tmp_path / "normalized.csv"
    pd.DataFrame([{"market": "SSE"}]).to_csv(normalized, index=False)
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "source": {"source_id": "sse", "dataset": "etf_scale"},
                "requested_date": "2026-07-15",
                "status_code": 200,
                "sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "artifacts": [
            _artifact(
                source=SSE_ETF,
                raw_path=raw,
                metadata_path=metadata,
                normalized_path=normalized,
                digest="0" * 64,
            )
        ]
    }

    findings = validate_artifact_lineage(manifest, Path("/"))
    digest = next(finding for finding in findings if finding.check_id == "lineage.sha256_matches")

    assert digest.status == "fail"


def test_release_qa_reconciles_compressed_same_day_pcf(tmp_path: Path) -> None:
    raw = tmp_path / "pcf.json.gz"
    payload = {
        "responses": [
            {
                "code": "588000",
                "observed_date": "2026-07-15",
                "basic": {
                    "FUNDID2": "588000",
                    "FUND_NAME": "科创50ETF",
                    "CREATION_REDEMPTION_UNIT": "3000000",
                },
                "components": [
                    {
                        "INSTRUMENT_ID": "688008",
                        "INSTRUMENT_NAME": "澜起科技",
                        "QUANTITY": "1813",
                        "SUBSTITUTION_FLAG": "1",
                    }
                ],
            }
        ]
    }
    raw.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode()))
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    normalized = tmp_path / "pcf.csv"
    pd.DataFrame(
        [
            {
                "trade_date": "2026-07-15",
                "pcf_date": "2026-07-15",
                "market": "SSE",
                "etf_code": "588000",
                "etf_name": "科创50ETF",
                "stock_code": "688008",
                "stock_name": "澜起科技",
                "quantity": 1813,
                "creation_unit": 3000000,
                "substitution_flag": "1",
                "listing_market": "",
                "source_id": "sse",
                "source_grade": "A",
                "date_basis": "sse_pcf_same_day_morning",
            }
        ]
    ).to_csv(normalized, index=False)

    artifact = _artifact(
        source=SSE_ETF_PCF,
        raw_path=raw,
        metadata_path=tmp_path / "metadata.json",
        normalized_path=normalized,
        digest=digest,
    )
    findings = validate_raw_to_normalized_mapping({"artifacts": [artifact]}, tmp_path)
    mapping = next(
        finding
        for finding in findings
        if finding.check_id == "mapping.raw_to_normalized_values"
    )

    assert mapping.status == "pass"


def test_release_qa_keeps_future_source_gap_as_pending_warning() -> None:
    finding = QualityFinding(
        check_id="timeliness.requested_date_coverage",
        dataset="szse_margin",
        status="fail",
        severity="high",
        message="未覆盖全部请求日期",
        evidence={"missing_dates": ["2026-07-31"]},
    )

    assert _future_only_failure(finding, "2026-07-30") is True
    pending = _pending_future_warning(finding)
    assert pending.status == "warn"
    assert "待补" in pending.message
    assert _future_only_failure(finding, "2026-07-31") is False


def test_normalized_storage_is_namespaced_by_source(tmp_path: Path) -> None:
    adapters = OfficialMarketAdapters(session=object(), data_root=tmp_path, run_id="run")  # type: ignore[arg-type]
    frame = pd.DataFrame([{"instrument_code": "510300"}])

    def persist(source) -> str:
        artifact = FetchArtifact(
            source=source,
            requested_date="2026-07-15",
            observed_at="2026-07-16T00:10:00+08:00",
            status_code=200,
            content_type="application/json",
            raw_path="raw",
            metadata_path="metadata",
            sha256="0" * 64,
        )
        _, saved = adapters._persist(frame, artifact, "2026-07-15")
        return str(saved.normalized_path)

    sse_path = persist(SSE_ETF)
    szse_path = persist(SZSE_ETF)

    assert sse_path != szse_path
    assert "sse_etf_scale" in sse_path
    assert "szse_etf_scale" in szse_path


def test_missing_hkex_top_name_is_warning_not_required_value_failure() -> None:
    manifest = {
        "requested_dates": ["2026-08-06"],
        "artifacts": [
            {
                "source": {"source_id": "hkex", "dataset": "stock_connect_daily"},
                "row_count": 2,
            }
        ],
    }
    frames = {
        "sse_etf": pd.DataFrame(),
        "szse_etf": pd.DataFrame(),
        "sse_margin": pd.DataFrame(),
        "szse_margin": pd.DataFrame(),
        "stock_connect_summary": pd.DataFrame(
            [
                {
                    "trade_date": "2026-08-06",
                    "market": "SSE",
                    "direction": "southbound",
                    "currency": "HKD",
                    "total_turnover": 100.0,
                    "total_trade_count": 1,
                    "etf_turnover": 0.0,
                    "source_id": "hkex",
                }
            ]
        ),
        "stock_connect_top": pd.DataFrame(
            [
                {
                    "trade_date": "2026-08-06",
                    "market": "SSE",
                    "direction": "southbound",
                    "rank": 1,
                    "instrument_code": "09999",
                    "instrument_name": None,
                    "total_turnover": 100.0,
                    "source_id": "hkex",
                }
            ]
        ),
    }

    findings = validate_combined_data(manifest, frames)
    required = next(
        item
        for item in findings
        if item.check_id == "mapping.required_values" and item.dataset == "stock_connect_top"
    )
    resolution = next(
        item
        for item in findings
        if item.check_id == "mapping.instrument_name_resolution"
    )

    assert required.status == "pass"
    assert resolution.status == "warn"
    assert resolution.evidence["unresolved_rows"] == 1


def test_release_qa_blocks_partial_etf_window_in_derived_terminal() -> None:
    snapshot = {
        "etfTerminal": {
            "availablePeriods": ["1d", "3d"],
            "instruments": [
                {
                    "code": "159813",
                    "market": "SZSE",
                    "periods": {"1d": -3.68, "3d": -3.68},
                    "periodCoverage": {"1d": 1, "3d": 1},
                    "estimatedCashPeriods": {"1d": -2.77, "3d": -2.77},
                    "cashPeriodCoverage": {"1d": 1, "3d": 1},
                }
            ],
            "groupPeriods": [],
        }
    }

    findings = validate_etf_terminal_data(snapshot)
    period = next(item for item in findings if item.check_id == "derived.etf_period_coverage")
    cash = next(item for item in findings if item.check_id == "derived.etf_cash_period_coverage")

    assert period.status == "fail"
    assert cash.status == "fail"
    assert any(issue["period"] == "3d" for issue in period.evidence["issues"])


def test_release_qa_accepts_complete_etf_window() -> None:
    snapshot = {
        "etfTerminal": {
            "availablePeriods": ["1d", "3d"],
            "instruments": [
                {
                    "code": "510300",
                    "market": "SSE",
                    "periods": {"1d": 1.0, "3d": 3.0},
                    "periodCoverage": {"1d": 1, "3d": 3},
                    "estimatedCashPeriods": {"1d": 2.0, "3d": 6.0},
                    "cashPeriodCoverage": {"1d": 1, "3d": 3},
                }
            ],
            "groupPeriods": [
                {
                    "name": "宽基",
                    "periods": {"1d": 1.0, "3d": 3.0},
                    "comparableCount": {"1d": 1, "3d": 1},
                }
            ],
        }
    }

    findings = validate_etf_terminal_data(snapshot)

    assert all(item.status == "pass" for item in findings)


def test_release_qa_blocks_etf_name_or_share_mismatch_against_raw_row() -> None:
    snapshot = {
        "etfTerminal": {
            "instruments": [
                {
                    "code": "510300",
                    "market": "SSE",
                    "name": "错误名称",
                    "shares": 99.0,
                }
            ]
        }
    }
    frames = {
        "sse_etf": pd.DataFrame(
            [
                {
                    "trade_date": "2026-08-06",
                    "instrument_code": "510300",
                    "instrument_name": "沪深300ETF",
                    "shares": 100_000_000,
                }
            ]
        ),
        "szse_etf": pd.DataFrame(),
    }

    findings = validate_etf_terminal_identity(snapshot, frames, "2026-08-06")

    assert next(item for item in findings if item.check_id == "derived.etf_identity_matches_raw").status == "fail"
    assert next(item for item in findings if item.check_id == "derived.etf_shares_matches_raw").status == "fail"


def test_release_qa_reconciles_canonical_etf_with_all_views() -> None:
    dates = ["2026-08-13", "2026-08-14"]
    snapshot = {
        "asOf": dates[-1],
        "etfTerminal": {
            "availablePeriods": ["1d"],
            "instruments": [
                {"code": "510300", "market": "SSE", "name": "沪深300ETF", "periods": {"1d": 1.0}},
                {"code": "159919", "market": "SZSE", "name": "沪深300ETF", "periods": {"1d": -0.5}},
            ],
            "groupPeriods": [
                {"name": "宽基", "periods": {"1d": 0.5}, "comparableCount": {"1d": 2}},
            ],
            "universe": {"total": 2, "classified": 2, "unclassified": 0},
        },
    }
    frames = {
        "sse_etf": pd.DataFrame(
            [
                {"trade_date": dates[0], "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 100_000_000},
                {"trade_date": dates[1], "instrument_code": "510300", "instrument_name": "沪深300ETF", "shares": 200_000_000},
            ]
        ),
        "szse_etf": pd.DataFrame(
            [
                {"trade_date": dates[0], "instrument_code": "159919", "instrument_name": "沪深300ETF", "shares": 100_000_000},
                {"trade_date": dates[1], "instrument_code": "159919", "instrument_name": "沪深300ETF", "shares": 50_000_000},
            ]
        ),
    }

    findings = validate_etf_cross_artifact_reconciliation(
        snapshot,
        frames,
        dates=dates,
        as_of=dates[-1],
    )

    assert findings[0].check_id == "reconciliation.etf_cross_artifact_identity"
    assert all(item.status == "pass" for item in findings)
