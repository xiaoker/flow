from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pandas as pd

from .models import QualityFinding


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
    status = "pass" if passed else ("warn" if warn_only else "fail")
    return QualityFinding(
        check_id=check_id,
        dataset=dataset,
        status=status,
        severity="info" if passed else severity,  # type: ignore[arg-type]
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


def profile(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "null_rates": {
            column: round(float(frame[column].isna().mean()), 6)
            for column in frame.columns
        },
        "distinct_trade_dates": sorted(
            frame.get("trade_date", pd.Series(dtype=str)).dropna().astype(str).unique().tolist()
        ),
    }


def check_required_columns(
    frame: pd.DataFrame, dataset: str, required: Iterable[str]
) -> list[QualityFinding]:
    required_set = set(required)
    missing = sorted(required_set - set(frame.columns))
    return [
        _finding(
            "schema.required_columns",
            dataset,
            not missing,
            message="必填字段齐全" if not missing else "缺少必填字段",
            evidence={"missing": missing, "required": sorted(required_set)},
            remediation="更新适配器映射并保留失败响应作为契约 fixture。",
        )
    ]


def check_empty(frame: pd.DataFrame, dataset: str) -> list[QualityFinding]:
    return [
        _finding(
            "completeness.non_empty",
            dataset,
            not frame.empty,
            message="数据集非空" if not frame.empty else "源站返回空数据集",
            evidence={"rows": int(len(frame))},
            severity="high",
            remediation="保持上一 ACTIVE 版本并在下次任务重试。",
        )
    ]


def check_unique(
    frame: pd.DataFrame, dataset: str, keys: list[str]
) -> list[QualityFinding]:
    if frame.empty or any(key not in frame.columns for key in keys):
        return []
    duplicates = int(frame.duplicated(keys, keep=False).sum())
    return [
        _finding(
            "uniqueness.business_key",
            dataset,
            duplicates == 0,
            message="业务主键唯一" if duplicates == 0 else "存在重复业务主键",
            evidence={"keys": keys, "duplicate_rows": duplicates},
            remediation="阻断发布，检查分页、重复抓取或主键定义。",
        )
    ]


def check_nonnegative(
    frame: pd.DataFrame, dataset: str, columns: Iterable[str]
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for column in columns:
        if column not in frame.columns:
            continue
        invalid = int((pd.to_numeric(frame[column], errors="coerce") < 0).sum())
        findings.append(
            _finding(
                f"validity.nonnegative.{column}",
                dataset,
                invalid == 0,
                message=f"{column} 无负数" if invalid == 0 else f"{column} 出现负数",
                evidence={"invalid_rows": invalid},
                remediation="核对单位、哨兵值和源站字段含义。",
            )
        )
    return findings


def validate_etf(frame: pd.DataFrame, dataset: str) -> list[QualityFinding]:
    findings = check_empty(frame, dataset)
    findings += check_required_columns(
        frame,
        dataset,
        ["trade_date", "market", "instrument_code", "shares", "date_basis"],
    )
    findings += check_unique(frame, dataset, ["trade_date", "market", "instrument_code"])
    findings += check_nonnegative(frame, dataset, ["shares", "nav"])
    if not frame.empty and "instrument_code" in frame.columns:
        invalid_codes = int(
            (~frame["instrument_code"].astype(str).str.fullmatch(r"\d{6}")).sum()
        )
        findings.append(
            _finding(
                "validity.instrument_code",
                dataset,
                invalid_codes == 0,
                message="ETF 代码格式正确" if invalid_codes == 0 else "ETF 代码格式异常",
                evidence={"invalid_rows": invalid_codes},
                remediation="检查 Excel 数值转换是否丢失前导零。",
            )
        )
    inferred_rows = (
        int(frame["date_basis"].astype(str).str.startswith("collector_inferred").sum())
        if "date_basis" in frame.columns
        else 0
    )
    findings.append(
        _finding(
            "timeliness.source_date",
            dataset,
            inferred_rows == 0,
            message="统计日期由源站提供" if inferred_rows == 0 else "统计日期由采集任务推断",
            evidence={"inferred_rows": inferred_rows, "total_rows": int(len(frame))},
            severity="high",
            warn_only=True,
            remediation="只从当日定时快照向前积累，不把推断日期用于历史回填。",
        )
    )
    return findings


def validate_margin(frame: pd.DataFrame, dataset: str) -> list[QualityFinding]:
    findings = check_empty(frame, dataset)
    required = [
        "trade_date",
        "market",
        "financing_balance_yuan",
        "financing_buy_yuan",
        "securities_lending_balance_yuan",
        "margin_total_balance_yuan",
    ]
    findings += check_required_columns(frame, dataset, required)
    findings += check_unique(frame, dataset, ["trade_date", "market"])
    findings += check_nonnegative(frame, dataset, required[2:])
    if not frame.empty and all(column in frame.columns for column in required[2:]):
        expected = frame["financing_balance_yuan"] + frame["securities_lending_balance_yuan"]
        delta = (frame["margin_total_balance_yuan"] - expected).abs()
        # SZSE publishes values rounded to RMB 100 million, so allow one rounding unit.
        invalid = int((delta > 100_000_000).sum())
        findings.append(
            _finding(
                "consistency.margin_total",
                dataset,
                invalid == 0,
                message="两融总额与分项一致" if invalid == 0 else "两融总额与分项不一致",
                evidence={
                    "invalid_rows": invalid,
                    "max_abs_delta_yuan": None if delta.empty else float(delta.max()),
                    "tolerance_yuan": 100_000_000,
                },
                remediation="核对深交所亿元单位和四舍五入，上交所应接近精确相等。",
            )
        )
    return findings


def validate_connect_summary(
    frame: pd.DataFrame, dataset: str = "stock_connect_summary"
) -> list[QualityFinding]:
    findings = check_empty(frame, dataset)
    findings += check_required_columns(
        frame,
        dataset,
        [
            "trade_date",
            "market",
            "direction",
            "currency",
            "total_turnover",
            "total_trade_count",
            "etf_turnover",
        ],
    )
    findings += check_unique(frame, dataset, ["trade_date", "market", "direction"])
    findings += check_nonnegative(
        frame,
        dataset,
        ["total_turnover", "buy_turnover", "sell_turnover", "etf_turnover"],
    )
    if not frame.empty:
        south = frame[frame["direction"] == "southbound"].copy()
        if not south.empty:
            delta = (south["total_turnover"] - south["buy_turnover"] - south["sell_turnover"]).abs()
            invalid = int((delta > 20_000).sum())
            findings.append(
                _finding(
                    "consistency.southbound_turnover",
                    dataset,
                    invalid == 0,
                    message="南向总成交额等于买入加卖出" if invalid == 0 else "南向成交额分项不一致",
                    evidence={"invalid_rows": invalid, "max_abs_delta": float(delta.max())},
                    remediation="检查 HKD 百万元到港元的单位转换和页面舍入。",
                )
            )
        north = frame[frame["direction"] == "northbound"]
        disclosed_buy_sell = int(north[["buy_turnover", "sell_turnover"]].notna().any(axis=1).sum())
        findings.append(
            _finding(
                "policy.northbound_no_net_flow",
                dataset,
                disclosed_buy_sell == 0,
                message="北向未伪造买卖额或净流入" if disclosed_buy_sell == 0 else "北向出现不应使用的买卖额",
                evidence={"rows_with_buy_or_sell": disclosed_buy_sell},
                remediation="2024-08-19 后仅展示官方总成交额、笔数、ETF 成交额和前十活跃证券。",
            )
        )
    return findings


def validate_connect_top(
    frame: pd.DataFrame, dataset: str = "stock_connect_top"
) -> list[QualityFinding]:
    findings = check_empty(frame, dataset)
    findings += check_required_columns(
        frame,
        dataset,
        ["trade_date", "market", "direction", "rank", "instrument_code", "total_turnover"],
    )
    findings += check_unique(
        frame, dataset, ["trade_date", "market", "direction", "rank"]
    )
    if not frame.empty:
        invalid_rank = int((~frame["rank"].between(1, 10)).sum())
        group_sizes = frame.groupby(["trade_date", "market", "direction"]).size()
        invalid_groups = int((group_sizes != 10).sum())
        findings.append(
            _finding(
                "completeness.top10",
                dataset,
                invalid_rank == 0 and invalid_groups == 0,
                message="每个通道包含完整前十" if invalid_rank == 0 and invalid_groups == 0 else "前十榜单不完整",
                evidence={
                    "invalid_rank_rows": invalid_rank,
                    "invalid_group_count": invalid_groups,
                    "group_sizes": {"|".join(map(str, key)): int(value) for key, value in group_sizes.items()},
                },
                severity="high",
                remediation="检查 HKEX 页面结构、交易日状态和解析逻辑。",
            )
        )
    return findings


def summarize_findings(findings: Iterable[QualityFinding]) -> dict[str, Any]:
    rows = [finding.to_dict() for finding in findings]
    counts = {status: sum(row["status"] == status for row in rows) for status in ("pass", "warn", "fail")}
    return {"counts": counts, "release_eligible": counts["fail"] == 0, "findings": rows}

