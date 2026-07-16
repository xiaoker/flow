from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd

from .dashboard import build_dashboard_snapshot
from .models import QualityFinding
from .quality import summarize_findings
from .storage import now_iso, write_json


APPROVED_SOURCE_HOSTS = {
    "sse": {"query.sse.com.cn", "www.sse.com.cn"},
    "szse": {"fund.szse.cn", "www.szse.cn"},
    "hkex": {"www.hkex.com.hk"},
}

COMBINED_DATASET_KEYS = {
    ("sse", "etf_scale"): ("sse_etf",),
    ("szse", "etf_scale"): ("szse_etf",),
    ("sse", "margin_market_daily"): ("sse_margin",),
    ("szse", "margin_market_daily"): ("szse_margin",),
    ("hkex", "stock_connect_daily"): (
        "stock_connect_summary",
        "stock_connect_top",
    ),
}

EXPECTED_CONTEXT = {
    "sse_etf": ("sse", {"SSE"}),
    "szse_etf": ("szse", {"SZSE"}),
    "sse_margin": ("sse", {"SSE"}),
    "szse_margin": ("szse", {"SZSE"}),
    "stock_connect_summary": ("hkex", {"SSE", "SZSE"}),
    "stock_connect_top": ("hkex", {"SSE", "SZSE"}),
}


def _finding(
    check_id: str,
    dataset: str,
    passed: bool,
    *,
    message: str,
    evidence: dict[str, Any],
    severity: str = "critical",
    warn_only: bool = False,
    remediation: str | None = None,
) -> QualityFinding:
    return QualityFinding(
        check_id=check_id,
        dataset=dataset,
        status="pass" if passed else ("warn" if warn_only else "fail"),
        severity="info" if passed else severity,  # type: ignore[arg-type]
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


def _paths(value: str | None) -> list[str]:
    return [] if not value else [part for part in value.split(";") if part]


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"instrument_code": str})


def _number(value: Any, multiplier: float = 1.0) -> float | None:
    if value is None or str(value).strip() in {"", "-", "--", "N/A", "NA"}:
        return None
    return float(str(value).replace(",", "").strip()) * multiplier


def _normalized_date(value: Any, fallback: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return fallback if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def _compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    keys: list[str],
    text_columns: list[str],
    numeric_columns: list[str],
) -> dict[str, Any]:
    required = set(keys + text_columns + numeric_columns)
    missing_columns = sorted(required - set(actual.columns))
    if missing_columns:
        return {
            "matched": False,
            "expected_rows": len(expected),
            "actual_rows": len(actual),
            "missing_columns": missing_columns,
            "missing_keys": len(expected),
            "extra_keys": len(actual),
            "value_mismatches": len(expected),
        }

    def indexed(frame: pd.DataFrame) -> dict[tuple[str, ...], pd.Series]:
        rows: dict[tuple[str, ...], pd.Series] = {}
        for _, row in frame.iterrows():
            key = tuple(str(row[column]) for column in keys)
            rows[key] = row
        return rows

    expected_rows = indexed(expected)
    actual_rows = indexed(actual)
    expected_keys = set(expected_rows)
    actual_keys = set(actual_rows)
    value_mismatches = 0
    sample_mismatches: list[dict[str, Any]] = []
    for key in sorted(expected_keys & actual_keys):
        expected_row = expected_rows[key]
        actual_row = actual_rows[key]
        row_mismatches: list[str] = []
        for column in text_columns:
            left = "" if pd.isna(expected_row[column]) else str(expected_row[column]).strip()
            right = "" if pd.isna(actual_row[column]) else str(actual_row[column]).strip()
            if left != right:
                row_mismatches.append(column)
        for column in numeric_columns:
            left = expected_row[column]
            right = actual_row[column]
            if pd.isna(left) and pd.isna(right):
                continue
            if pd.isna(left) or pd.isna(right) or not math.isclose(
                float(left), float(right), rel_tol=1e-12, abs_tol=0.01
            ):
                row_mismatches.append(column)
        if row_mismatches:
            value_mismatches += 1
            if len(sample_mismatches) < 5:
                sample_mismatches.append({"key": list(key), "columns": row_mismatches})

    missing_keys = expected_keys - actual_keys
    extra_keys = actual_keys - expected_keys
    return {
        "matched": not missing_columns and not missing_keys and not extra_keys and value_mismatches == 0,
        "expected_rows": len(expected),
        "actual_rows": len(actual),
        "missing_columns": missing_columns,
        "missing_keys": len(missing_keys),
        "extra_keys": len(extra_keys),
        "value_mismatches": value_mismatches,
        "sample_mismatches": sample_mismatches,
    }


def _expected_sse_etf(content: bytes, requested_date: str) -> pd.DataFrame:
    rows = json.loads(content).get("result") or []
    return pd.DataFrame(
        [
            {
                "trade_date": _normalized_date(row.get("STAT_DATE"), requested_date),
                "market": "SSE",
                "instrument_code": str(row.get("SEC_CODE", "")).zfill(6),
                "instrument_name": row.get("SEC_NAME"),
                "shares": _number(row.get("TOT_VOL"), 10_000),
            }
            for row in rows
        ]
    )


def _expected_szse_etf(content: bytes, inferred_date: str) -> pd.DataFrame:
    frame = pd.read_excel(io.BytesIO(content), engine="openpyxl", dtype={"基金代码": str})
    return pd.DataFrame(
        {
            "trade_date": inferred_date,
            "market": "SZSE",
            "instrument_code": frame["基金代码"].astype(str).str.zfill(6),
            "instrument_name": frame["基金简称"],
            "shares": pd.to_numeric(frame["当前规模(份)"].astype(str).str.replace(",", "", regex=False), errors="coerce"),
            "nav": pd.to_numeric(frame.get("净值"), errors="coerce"),
        }
    )


def _expected_sse_margin(content: bytes) -> pd.DataFrame:
    rows = json.loads(content).get("result") or []
    return pd.DataFrame(
        [
            {
                "trade_date": pd.to_datetime(row.get("opDate"), format="%Y%m%d").strftime("%Y-%m-%d"),
                "market": "SSE",
                "financing_balance_yuan": _number(row.get("rzye")),
                "financing_buy_yuan": _number(row.get("rzmre")),
                "securities_lending_balance_yuan": _number(row.get("rqylje")),
                "margin_total_balance_yuan": _number(row.get("rzrqjyzl")),
            }
            for row in rows
        ]
    )


def _expected_szse_margin(content: bytes, requested_date: str) -> pd.DataFrame:
    payload = json.loads(content)
    rows = payload[0].get("data", []) if payload else []
    return pd.DataFrame(
        [
            {
                "trade_date": requested_date,
                "market": "SZSE",
                "financing_balance_yuan": _number(row.get("jrrzye"), 100_000_000),
                "financing_buy_yuan": _number(row.get("jrrzmr"), 100_000_000),
                "securities_lending_balance_yuan": _number(row.get("jrrjye"), 100_000_000),
                "margin_total_balance_yuan": _number(row.get("jrrzrjye"), 100_000_000),
            }
            for row in rows
        ]
    )


def _expected_hkex(content: bytes, requested_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    text = content.decode("utf-8").strip()
    match = re.fullmatch(r"tabData\s*=\s*(\[.*\])\s*;?", text, flags=re.DOTALL)
    if not match:
        raise ValueError("HKEX raw wrapper does not match tabData JSON")
    summary_rows: list[dict[str, Any]] = []
    top_rows: list[dict[str, Any]] = []
    for payload in json.loads(match.group(1)):
        if not payload.get("tradingDay"):
            continue
        label = str(payload.get("market", ""))
        market = "SSE" if label.startswith("SSE") else "SZSE"
        direction = "northbound" if "Northbound" in label else "southbound"
        currency = "RMB" if direction == "northbound" else "HKD"
        trade_date = payload.get("date") or requested_date
        for content_block in payload.get("content", []):
            table = content_block.get("table", {})
            schema = (table.get("schema") or [[]])[0]
            if content_block.get("style") == 1:
                values = [row.get("td", [[None]])[0][0] for row in table.get("tr", [])]
                raw = dict(zip(schema, values))
                buy = _number(raw.get("Buy Turnover"), 1_000_000)
                sell = _number(raw.get("Sell Turnover"), 1_000_000)
                summary_rows.append(
                    {
                        "trade_date": trade_date,
                        "market": market,
                        "direction": direction,
                        "currency": currency,
                        "total_turnover": _number(raw.get("Total Turnover"), 1_000_000),
                        "buy_turnover": buy,
                        "sell_turnover": sell,
                        "net_buy": None if buy is None or sell is None else buy - sell,
                        "total_trade_count": _number(raw.get("Total Trade Count")),
                        "etf_turnover": _number(raw.get("ETF Turnover"), 1_000_000),
                    }
                )
            elif content_block.get("style") == 2:
                for row in table.get("tr", []):
                    values = row.get("td", [[]])[0]
                    raw = dict(zip(schema, values))
                    top_rows.append(
                        {
                            "trade_date": trade_date,
                            "market": market,
                            "direction": direction,
                            "rank": None if _number(raw.get("Rank")) is None else int(_number(raw.get("Rank"))),
                            "instrument_code": str(raw.get("Stock Code", "")),
                            "instrument_name": raw.get("Stock Name"),
                            "buy_turnover": _number(raw.get("Buy Turnover")),
                            "sell_turnover": _number(raw.get("Sell Turnover")),
                            "total_turnover": _number(raw.get("Total Turnover")),
                        }
                    )
    return pd.DataFrame(summary_rows), pd.DataFrame(top_rows)


def validate_raw_to_normalized_mapping(
    manifest: dict[str, Any], workspace_root: Path
) -> list[QualityFinding]:
    """Independently reconcile source fields against every normalized row."""

    findings: list[QualityFinding] = []
    inferred_date = str(manifest.get("szse_etf_inferred_date", ""))
    for artifact in manifest.get("artifacts") or []:
        source = artifact.get("source") or {}
        source_id = str(source.get("source_id", ""))
        dataset = str(source.get("dataset", ""))
        requested_date = str(artifact.get("requested_date") or inferred_date)
        label = f"{source_id}.{dataset}:{artifact.get('requested_date') or 'current'}"
        raw_path = workspace_root / str(artifact.get("raw_path", ""))
        normalized_paths = [workspace_root / path for path in _paths(artifact.get("normalized_path"))]
        result: dict[str, Any]
        try:
            content = raw_path.read_bytes()
            if (source_id, dataset) == ("sse", "etf_scale"):
                result = _compare_frames(
                    _expected_sse_etf(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "instrument_code"],
                    text_columns=["instrument_name"],
                    numeric_columns=["shares"],
                )
            elif (source_id, dataset) == ("szse", "etf_scale"):
                result = _compare_frames(
                    _expected_szse_etf(content, inferred_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market", "instrument_code"],
                    text_columns=["instrument_name"],
                    numeric_columns=["shares", "nav"],
                )
            elif (source_id, dataset) == ("sse", "margin_market_daily"):
                result = _compare_frames(
                    _expected_sse_margin(content),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market"],
                    text_columns=[],
                    numeric_columns=["financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan"],
                )
            elif (source_id, dataset) == ("szse", "margin_market_daily"):
                result = _compare_frames(
                    _expected_szse_margin(content, requested_date),
                    _read_csv(normalized_paths[0]),
                    keys=["trade_date", "market"],
                    text_columns=[],
                    numeric_columns=["financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan"],
                )
            elif (source_id, dataset) == ("hkex", "stock_connect_daily"):
                expected_summary, expected_top = _expected_hkex(content, requested_date)
                actual_frames = [_read_csv(path) for path in normalized_paths]
                actual_summary = next(frame for frame in actual_frames if "rank" not in frame.columns)
                actual_top = next(frame for frame in actual_frames if "rank" in frame.columns)
                summary_result = _compare_frames(
                    expected_summary,
                    actual_summary,
                    keys=["trade_date", "market", "direction"],
                    text_columns=["currency"],
                    numeric_columns=["total_turnover", "buy_turnover", "sell_turnover", "net_buy", "total_trade_count", "etf_turnover"],
                )
                top_result = _compare_frames(
                    expected_top,
                    actual_top,
                    keys=["trade_date", "market", "direction", "rank"],
                    text_columns=["instrument_code", "instrument_name"],
                    numeric_columns=["buy_turnover", "sell_turnover", "total_turnover"],
                )
                result = {
                    "matched": summary_result["matched"] and top_result["matched"],
                    "summary": summary_result,
                    "top10": top_result,
                }
            else:
                result = {"matched": False, "error": "unsupported source dataset"}
        except (OSError, ValueError, KeyError, IndexError, StopIteration, TypeError) as exc:
            result = {"matched": False, "error": f"{type(exc).__name__}: {exc}"}
        findings.append(
            _finding(
                "mapping.raw_to_normalized_values",
                label,
                bool(result.get("matched")),
                message="原始字段与规范化键值逐行一致" if result.get("matched") else "原始字段与规范化结果不一致",
                evidence=result,
                remediation="阻断发布，核对字段名、单位倍率、日期和业务键；禁止按行号拼接。",
            )
        )
    return findings


def validate_artifact_lineage(
    manifest: dict[str, Any], workspace_root: Path
) -> list[QualityFinding]:
    """Validate that every manifest claim can be reproduced from files on disk."""

    findings: list[QualityFinding] = []
    artifacts = manifest.get("artifacts") or []
    findings.append(
        _finding(
            "lineage.artifacts_present",
            "manifest",
            bool(artifacts),
            message="采集证据清单非空" if artifacts else "采集证据清单为空",
            evidence={"artifact_count": len(artifacts)},
            remediation="阻断发布并重新执行官方数据采集。",
        )
    )

    normalized_owners: dict[str, list[str]] = {}
    observed_sources: set[str] = set()
    for index, artifact in enumerate(artifacts):
        source = artifact.get("source") or {}
        source_id = str(source.get("source_id", ""))
        dataset = str(source.get("dataset", ""))
        requested_date = artifact.get("requested_date") or "current"
        label = f"{source_id}.{dataset}:{requested_date}"
        observed_sources.add(source_id)

        parsed = urlparse(str(source.get("url", "")))
        approved = (
            parsed.scheme == "https"
            and parsed.hostname in APPROVED_SOURCE_HOSTS.get(source_id, set())
            and source.get("source_grade") == "A"
        )
        findings.append(
            _finding(
                "provenance.approved_official_source",
                label,
                approved,
                message="来源域名和来源等级可信" if approved else "来源不在官方白名单",
                evidence={
                    "source_id": source_id,
                    "url": source.get("url"),
                    "source_grade": source.get("source_grade"),
                },
                remediation="只允许 HTTPS 的上交所、深交所和港交所官方域名。",
            )
        )

        raw_path = workspace_root / str(artifact.get("raw_path", ""))
        metadata_path = workspace_root / str(artifact.get("metadata_path", ""))
        files_exist = raw_path.is_file() and metadata_path.is_file()
        findings.append(
            _finding(
                "lineage.raw_and_metadata_exist",
                label,
                files_exist,
                message="原始响应和元数据均存在" if files_exist else "原始证据文件缺失",
                evidence={
                    "raw_path": str(artifact.get("raw_path", "")),
                    "metadata_path": str(artifact.get("metadata_path", "")),
                },
                remediation="保留上一有效快照，重新采集缺失来源。",
            )
        )

        digest_matches = False
        metadata_matches = False
        if files_exist:
            digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            digest_matches = digest == artifact.get("sha256")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata_matches = all(
                    (
                        metadata.get("sha256") == artifact.get("sha256"),
                        metadata.get("status_code") == artifact.get("status_code"),
                        metadata.get("requested_date") == artifact.get("requested_date"),
                        (metadata.get("source") or {}).get("source_id") == source_id,
                        (metadata.get("source") or {}).get("dataset") == dataset,
                    )
                )
            except (OSError, ValueError, TypeError):
                metadata_matches = False
        findings.append(
            _finding(
                "lineage.sha256_matches",
                label,
                digest_matches,
                message="原始响应哈希可复验" if digest_matches else "原始响应哈希不一致",
                evidence={"expected_sha256": artifact.get("sha256")},
                remediation="将证据视为被修改或损坏，阻断发布并重新采集。",
            )
        )
        findings.append(
            _finding(
                "lineage.metadata_matches_manifest",
                label,
                metadata_matches,
                message="元数据与清单一致" if metadata_matches else "元数据与清单不一致",
                evidence={"artifact_index": index},
                remediation="检查请求日期、来源标识和清单写入流程。",
            )
        )

        normalized_paths = _paths(artifact.get("normalized_path"))
        normalized_rows = 0
        normalized_ok = bool(normalized_paths)
        for relative_path in normalized_paths:
            normalized_owners.setdefault(relative_path, []).append(label)
            path = workspace_root / relative_path
            if not path.is_file():
                normalized_ok = False
                continue
            try:
                normalized_rows += len(_read_csv(path))
            except (OSError, ValueError, pd.errors.ParserError):
                normalized_ok = False
        expected_rows = int(artifact.get("row_count") or 0)
        normalized_ok = normalized_ok and normalized_rows == expected_rows
        findings.append(
            _finding(
                "lineage.normalized_row_count",
                label,
                normalized_ok,
                message="规范化行数与解析结果一致" if normalized_ok else "规范化文件缺失、被覆盖或行数不符",
                evidence={
                    "expected_rows": expected_rows,
                    "observed_rows": normalized_rows,
                    "paths": normalized_paths,
                },
                remediation="为每个交易所使用独立存储命名空间，并重新采集本批数据。",
            )
        )

    collisions = {
        path: owners for path, owners in normalized_owners.items() if len(owners) > 1
    }
    findings.append(
        _finding(
            "lineage.normalized_paths_unique",
            "manifest",
            not collisions,
            message="各来源规范化路径互不覆盖" if not collisions else "不同来源复用了同一规范化路径",
            evidence={"collisions": collisions},
            remediation="将 source_id 纳入规范化文件目录，并重新采集。",
        )
    )
    missing_sources = sorted(set(APPROVED_SOURCE_HOSTS) - observed_sources)
    findings.append(
        _finding(
            "provenance.required_publishers",
            "manifest",
            not missing_sources,
            message="三家交易所来源齐全" if not missing_sources else "缺少交易所来源",
            evidence={"observed": sorted(observed_sources), "missing": missing_sources},
            remediation="ETF、两融和互联互通任一官方来源缺失时不发布。",
        )
    )
    return findings


def _load_combined_frames(
    manifest: dict[str, Any], workspace_root: Path
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for name in EXPECTED_CONTEXT:
        relative_path = (manifest.get("combined_paths") or {}).get(name)
        if not relative_path or not (workspace_root / relative_path).is_file():
            missing.append(name)
            frames[name] = pd.DataFrame()
            continue
        frames[name] = _read_csv(workspace_root / relative_path)
    return frames, missing


def validate_combined_data(
    manifest: dict[str, Any], frames: dict[str, pd.DataFrame]
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    requested_dates = sorted(str(value) for value in manifest.get("requested_dates", []))

    artifact_counts: dict[tuple[str, str], int] = {}
    for artifact in manifest.get("artifacts") or []:
        source = artifact.get("source") or {}
        key = (str(source.get("source_id", "")), str(source.get("dataset", "")))
        artifact_counts[key] = artifact_counts.get(key, 0) + int(artifact.get("row_count") or 0)
    for source_key, combined_keys in COMBINED_DATASET_KEYS.items():
        expected = artifact_counts.get(source_key, 0)
        observed = sum(len(frames.get(key, pd.DataFrame())) for key in combined_keys)
        findings.append(
            _finding(
                "lineage.combined_row_count",
                ".".join(source_key),
                expected == observed and expected > 0,
                message="合并明细未丢行" if expected == observed and expected > 0 else "合并明细行数与单源解析不一致",
                evidence={
                    "expected_rows": expected,
                    "observed_rows": observed,
                    "combined_datasets": list(combined_keys),
                },
                remediation="阻断发布，检查合并键、空表处理和跨来源覆盖。",
            )
        )

    required_values = {
        "sse_etf": ["trade_date", "market", "instrument_code", "instrument_name", "shares", "source_id", "source_grade"],
        "szse_etf": ["trade_date", "market", "instrument_code", "instrument_name", "shares", "source_id", "source_grade"],
        "sse_margin": ["trade_date", "market", "financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan", "source_id"],
        "szse_margin": ["trade_date", "market", "financing_balance_yuan", "financing_buy_yuan", "securities_lending_balance_yuan", "margin_total_balance_yuan", "source_id"],
        "stock_connect_summary": ["trade_date", "market", "direction", "currency", "total_turnover", "total_trade_count", "etf_turnover", "source_id"],
        "stock_connect_top": ["trade_date", "market", "direction", "rank", "instrument_code", "instrument_name", "total_turnover", "source_id"],
    }
    for name, (source_id, markets) in EXPECTED_CONTEXT.items():
        frame = frames.get(name, pd.DataFrame())
        missing_columns = sorted(set(required_values[name]) - set(frame.columns))
        null_rows = 0 if missing_columns else int(frame[required_values[name]].isna().any(axis=1).sum())
        invalid_context = len(frame)
        if not frame.empty and {"source_id", "market"}.issubset(frame.columns):
            invalid_context = int(
                ((frame["source_id"].astype(str) != source_id) | (~frame["market"].isin(markets))).sum()
            )
        findings.append(
            _finding(
                "mapping.required_values",
                name,
                not missing_columns and null_rows == 0,
                message="匹配键和核心数值完整" if not missing_columns and null_rows == 0 else "匹配键或核心数值缺失",
                evidence={"missing_columns": missing_columns, "rows_with_nulls": null_rows},
                remediation="不要用行号拼接；以日期、市场、代码等业务键重新映射。",
            )
        )
        findings.append(
            _finding(
                "mapping.source_market_consistency",
                name,
                invalid_context == 0 and not frame.empty,
                message="来源与市场映射一致" if invalid_context == 0 and not frame.empty else "来源与市场映射错位",
                evidence={"invalid_rows": invalid_context, "expected_source": source_id, "expected_markets": sorted(markets)},
                remediation="阻断发布并检查适配器常量和跨市场合并。",
            )
        )

    sse_etf = frames.get("sse_etf", pd.DataFrame())
    counts = (
        sse_etf.groupby("trade_date")["instrument_code"].nunique().astype(int).to_dict()
        if {"trade_date", "instrument_code"}.issubset(sse_etf.columns)
        else {}
    )
    ratio = min(counts.values()) / max(counts.values()) if counts and max(counts.values()) else 0.0
    findings.append(
        _finding(
            "volume.sse_etf_daily_stability",
            "sse_etf",
            set(counts) == set(requested_dates) and ratio >= 0.8,
            message="ETF 日覆盖量稳定" if set(counts) == set(requested_dates) and ratio >= 0.8 else "ETF 日覆盖量异常跳变",
            evidence={"daily_unique_codes": counts, "min_max_ratio": round(ratio, 4)},
            severity="high",
            remediation="检查分页、交易日和源站空响应；不要发布断崖式缺行快照。",
        )
    )

    connect = frames.get("stock_connect_summary", pd.DataFrame())
    expected_channels = {("SSE", "northbound"), ("SSE", "southbound"), ("SZSE", "northbound"), ("SZSE", "southbound")}
    invalid_channel_dates: dict[str, list[str]] = {}
    if {"trade_date", "market", "direction", "currency"}.issubset(connect.columns):
        for trade_date, group in connect.groupby("trade_date"):
            channels = set(zip(group["market"].astype(str), group["direction"].astype(str)))
            currency_ok = bool(
                ((group["direction"].eq("northbound") & group["currency"].eq("RMB"))
                 | (group["direction"].eq("southbound") & group["currency"].eq("HKD"))).all()
            )
            if channels != expected_channels or not currency_ok:
                invalid_channel_dates[str(trade_date)] = [f"{market}:{direction}" for market, direction in sorted(channels)]
    channels_ok = set(connect.get("trade_date", pd.Series(dtype=str)).astype(str).unique()) == set(requested_dates) and not invalid_channel_dates
    findings.append(
        _finding(
            "mapping.connect_channels",
            "stock_connect_summary",
            channels_ok,
            message="每日四通道和币种映射完整" if channels_ok else "互联互通通道或币种映射异常",
            evidence={"invalid_dates": invalid_channel_dates, "expected_dates": requested_dates},
            remediation="按 market + direction 业务键核对四通道，不以页面顺序匹配。",
        )
    )
    return findings


def _all_numbers_finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, dict):
        return all(_all_numbers_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_numbers_finite(item) for item in value)
    return True


def _metric_value(snapshot: dict[str, Any], metric_id: str) -> float:
    metric = next(item for item in snapshot.get("metrics", []) if item.get("id") == metric_id)
    return float(str(metric["value"]).replace(",", ""))


def validate_snapshot_values(
    manifest: dict[str, Any],
    quality: dict[str, Any],
    snapshot: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    *,
    manifest_path: Path,
    quality_path: Path,
    workspace_root: Path,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    dates = sorted(str(value) for value in manifest.get("requested_dates", []))
    as_of = dates[-1] if dates else None
    expected_snapshot = build_dashboard_snapshot(
        manifest_path=manifest_path,
        quality_path=quality_path,
        workspace_root=workspace_root,
    )
    exact_match = snapshot == expected_snapshot
    findings.append(
        _finding(
            "publication.snapshot_reproducible",
            "dashboard_snapshot",
            exact_match,
            message="发布快照可由已验证明细逐字节重建" if exact_match else "发布快照与明细重建结果不一致",
            evidence={"as_of": snapshot.get("asOf"), "expected_as_of": as_of},
            remediation="重新生成快照，禁止手工修改发布 JSON。",
        )
    )
    findings.append(
        _finding(
            "publication.snapshot_finite",
            "dashboard_snapshot",
            _all_numbers_finite(snapshot),
            message="页面快照不含 NaN 或无穷值" if _all_numbers_finite(snapshot) else "页面快照包含非有限数值",
            evidence={},
            remediation="在计算层处理缺失值，不允许 NaN/Infinity 进入 JSON。",
        )
    )

    headline_ok = False
    evidence: dict[str, Any] = {}
    try:
        sse_etf = frames["sse_etf"]
        latest = sse_etf[sse_etf["trade_date"].astype(str).eq(dates[-1])][["instrument_code", "shares"]]
        previous = sse_etf[sse_etf["trade_date"].astype(str).eq(dates[-2])][["instrument_code", "shares"]]
        comparable = latest.merge(previous, on="instrument_code", suffixes=("_latest", "_previous"))
        etf_expected = round(float((comparable["shares_latest"] - comparable["shares_previous"]).sum()) / 100_000_000, 2)

        margin = pd.concat([frames["sse_margin"], frames["szse_margin"]], ignore_index=True)
        margin_expected = round(float(margin[margin["trade_date"].astype(str).eq(as_of)]["financing_balance_yuan"].sum()) / 1_000_000_000_000, 4)
        connect = frames["stock_connect_summary"]
        current = connect[connect["trade_date"].astype(str).eq(as_of)]
        south_expected = round(float(current[current["direction"].eq("southbound")]["net_buy"].sum()) / 100_000_000, 2)
        north_expected = round(float(current[current["direction"].eq("northbound")]["total_turnover"].sum()) / 100_000_000, 2)
        actual = {
            "etf": _metric_value(snapshot, "etf"),
            "southbound": _metric_value(snapshot, "southbound"),
            "margin": _metric_value(snapshot, "margin"),
            "northbound": _metric_value(snapshot, "northbound"),
        }
        expected = {
            "etf": etf_expected,
            "southbound": south_expected,
            "margin": margin_expected,
            "northbound": north_expected,
        }
        tolerances = {"etf": 0.01, "southbound": 0.01, "margin": 0.0001, "northbound": 0.01}
        headline_ok = all(abs(actual[key] - expected[key]) <= tolerances[key] for key in expected)
        evidence = {"expected": expected, "actual": actual, "tolerances": tolerances}
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        headline_ok = False
        evidence = {"error": "headline reconciliation could not be computed"}
    findings.append(
        _finding(
            "reconciliation.dashboard_headlines",
            "dashboard_snapshot",
            headline_ok,
            message="四个首页指标可由明细独立反算" if headline_ok else "首页指标无法与明细对账",
            evidence=evidence,
            remediation="阻断发布，核对日期、单位和汇总公式。",
        )
    )

    base_gate_ok = int((quality.get("counts") or {}).get("fail", 0)) == 0 and quality.get("phase0_gate_status") != "blocked"
    findings.append(
        _finding(
            "publication.base_quality_gate",
            "quality_summary",
            base_gate_ok,
            message="基础质量门禁允许发布" if base_gate_ok else "基础质量门禁已阻断",
            evidence={"counts": quality.get("counts"), "gate": quality.get("phase0_gate_status")},
            remediation="先修复基础质量失败项，再运行发布 QA。",
        )
    )
    return findings


def run_release_qa(
    *,
    manifest_path: Path,
    quality_path: Path,
    snapshot_path: Path,
    output_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    findings: list[QualityFinding] = []
    findings.extend(validate_artifact_lineage(manifest, workspace_root))
    findings.extend(validate_raw_to_normalized_mapping(manifest, workspace_root))
    frames, missing = _load_combined_frames(manifest, workspace_root)
    findings.append(
        _finding(
            "lineage.combined_files_exist",
            "manifest",
            not missing,
            message="全部合并明细存在" if not missing else "合并明细缺失",
            evidence={"missing": missing},
            remediation="不发布缺失任一数据域的快照。",
        )
    )
    findings.extend(validate_combined_data(manifest, frames))
    if not missing:
        findings.extend(
            validate_snapshot_values(
                manifest,
                quality,
                snapshot,
                frames,
                manifest_path=manifest_path,
                quality_path=quality_path,
                workspace_root=workspace_root,
            )
        )

    summary = summarize_findings(findings)
    summary.update(
        {
            "run_id": manifest.get("run_id"),
            "generated_at": now_iso(),
            "verdict": "pass" if summary["counts"]["fail"] == 0 else "blocked",
            "known_limitations": [
                "深交所 ETF 文件不含统计日期，日期来自采集时点推断，只能从上线日起向前留存。",
                "ETF 页面当前展示份额变化，不等同于精确申赎金额。",
                "北向自披露调整后不提供买入额、卖出额或净流入，页面只展示官方成交总额。",
            ],
        }
    )
    write_json(output_path, summary)
    return summary


def failed_findings(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return (item for item in report.get("findings", []) if item.get("status") == "fail")
