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


def check_empty(
    frame: pd.DataFrame, dataset: str, *, warn_only: bool = False
) -> list[QualityFinding]:
    return [
        _finding(
            "completeness.non_empty",
            dataset,
            not frame.empty,
            message="数据集非空" if not frame.empty else "源站返回空数据集",
            evidence={"rows": int(len(frame))},
            severity="high",
            warn_only=warn_only,
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


def validate_etf_nav(frame: pd.DataFrame, dataset: str = "sse_etf_nav") -> list[QualityFinding]:
    """Validate the independent official NAV feed before it is joined to shares."""
    # NAV is an optional enrichment of the ETF share snapshot.  The SSE public
    # bulletin endpoint can legitimately have no exact-date row (for example
    # when a historical day was not captured at publication time).  Keep that
    # evidence visible, but do not block publication of the underlying shares.
    findings = [
        _finding(
            "completeness.non_empty",
            dataset,
            not frame.empty,
            message="官方 NAV 数据集非空" if not frame.empty else "该日期暂无精确匹配的官方 NAV",
            evidence={"rows": int(len(frame)), "optional_enrichment": True},
            severity="high",
            warn_only=True,
            remediation="保留空覆盖记录，等待下一次官方公告采集；金额估算在覆盖恢复前保持不可用。",
        )
    ]
    required = ["trade_date", "nav_date", "market", "instrument_code", "nav", "date_basis"]
    if frame.empty:
        missing = sorted(set(required) - set(frame.columns))
        findings.append(
            _finding(
                "schema.required_columns",
                dataset,
                not missing,
                message="空 NAV 响应保留标准字段" if not missing else "空 NAV 响应未带标准字段",
                evidence={"missing": missing, "required": sorted(required), "optional_enrichment": True},
                severity="high",
                warn_only=True,
                remediation="保留空响应的原始证据；有记录时恢复标准化字段校验。",
            )
        )
    else:
        findings += check_required_columns(frame, dataset, required)
    findings += check_unique(frame, dataset, ["trade_date", "market", "instrument_code"])
    findings += check_nonnegative(frame, dataset, ["nav"])
    if not frame.empty and "market" in frame.columns:
        invalid_market = int((frame["market"].astype(str) != "SSE").sum())
        findings.append(
            _finding(
                "validity.etf_nav_market",
                dataset,
                invalid_market == 0,
                message="NAV 来源市场为上交所" if invalid_market == 0 else "NAV 来源市场异常",
                evidence={"invalid_rows": invalid_market},
                remediation="检查上交所 NAV 分类请求是否混入其他市场。",
            )
        )
    if not frame.empty and {"trade_date", "nav_date"}.issubset(frame.columns):
        nav_dates = pd.to_datetime(frame["nav_date"], errors="coerce")
        trade_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        invalid_dates = int(nav_dates.isna().sum() + trade_dates.isna().sum())
        findings.append(
            _finding(
                "validity.etf_nav_dates",
                dataset,
                invalid_dates == 0,
                message="NAV 日期可解析" if invalid_dates == 0 else "NAV 日期不可解析",
                evidence={"invalid_rows": invalid_dates},
                remediation="保留公告日与净值日，禁止把公告日当作净值日。",
            )
        )
        invalid_order = int((nav_dates.ge(trade_dates)).fillna(False).sum())
        findings.append(
            _finding(
                "consistency.etf_nav_previous_day",
                dataset,
                invalid_order == 0,
                message="单位净值日早于公告交易日" if invalid_order == 0 else "单位净值日不早于公告交易日",
                evidence={"invalid_rows": invalid_order},
                remediation="检查公告日与净值日字段映射；不得使用未来或同日净值。",
            )
        )
    return findings


def validate_etf_pcf(frame: pd.DataFrame, dataset: str = "sse_etf_pcf") -> list[QualityFinding]:
    """Validate the optional ETF PCF enrichment feed.

    PCF is intentionally optional for the base dashboard.  An empty response
    is therefore a warning, while malformed or duplicate component keys are a
    hard failure whenever rows are present.  A next-day PCF is valid only when
    its official ``PRE_TRADING_DAY`` equals the ETF share date.
    """
    findings = [
        _finding(
            "completeness.pcf_non_empty",
            dataset,
            not frame.empty,
            message="PCF 非空" if not frame.empty else "PCF 尚未归档",
            evidence={"rows": int(len(frame)), "optional_enrichment": True},
            severity="high",
            warn_only=True,
            remediation="仅接受同日 PCF，或接受其 PRE_TRADING_DAY 与份额日一致的次日披露篮子；不使用无关日期替代。",
        )
    ]
    required = [
        "trade_date", "market", "etf_code", "stock_code", "quantity",
        "creation_unit", "date_basis",
    ]
    if frame.empty:
        findings.append(
            _finding(
                "schema.pcf_required_columns",
                dataset,
                not (set(required) - set(frame.columns)),
                message="空 PCF 响应保留标准字段" if not (set(required) - set(frame.columns)) else "空 PCF 响应字段缺失",
                evidence={"missing": sorted(set(required) - set(frame.columns))},
                severity="high",
                warn_only=True,
                remediation="保留原始响应；恢复同日或 PRE_TRADING_DAY 对齐后再启用穿透计算。",
            )
        )
        return findings
    findings += check_required_columns(frame, dataset, required)
    pcf_key = ["trade_date", "market", "etf_code"]
    if "listing_market" in frame.columns:
        pcf_key.append("listing_market")
    pcf_key.append("stock_code")
    findings += check_unique(frame, dataset, pcf_key)
    findings += check_nonnegative(frame, dataset, ["quantity", "creation_unit"])
    if "trade_date" in frame.columns:
        parsed = pd.to_datetime(frame["trade_date"], errors="coerce")
        invalid = int(parsed.isna().sum())
        findings.append(
            _finding(
                "validity.pcf_date",
                dataset,
                invalid == 0,
                message="份额日与 PCF 发布日可解析" if invalid == 0 else "PCF 日期不可解析",
                evidence={"invalid_rows": invalid},
                remediation="检查交易所 TRADING_DAY/PRE_TRADING_DAY 字段，不允许用采集日期替代披露日期。",
            )
        )
    if "pcf_date" in frame.columns:
        pcf_dates = pd.to_datetime(frame["pcf_date"], errors="coerce")
        invalid_pcf = int(pcf_dates.isna().sum())
        findings.append(
            _finding(
                "validity.pcf_published_date",
                dataset,
                invalid_pcf == 0,
                message="PCF 发布日可解析" if invalid_pcf == 0 else "PCF 发布日不可解析",
                evidence={"invalid_rows": invalid_pcf},
                remediation="保留交易所 TRADING_DAY 作为 PCF 发布日。",
            )
        )
    if "pcf_previous_date" in frame.columns and "date_basis" in frame.columns:
        previous = pd.to_datetime(frame["pcf_previous_date"], errors="coerce")
        effective = pd.to_datetime(frame["trade_date"], errors="coerce")
        next_day_rows = frame["date_basis"].astype(str).eq("sse_pcf_next_day_previous_trading_day")
        invalid_previous = int((next_day_rows & (previous.isna() | previous.ne(effective))).sum())
        findings.append(
            _finding(
                "consistency.pcf_previous_trading_day",
                dataset,
                invalid_previous == 0,
                message="次日 PCF 的 PRE_TRADING_DAY 与份额日一致" if invalid_previous == 0 else "次日 PCF 的 PRE_TRADING_DAY 与份额日不一致",
                evidence={"invalid_rows": invalid_previous},
                remediation="只保留 PRE_TRADING_DAY 等于份额日的次日 PCF。",
            )
        )
    return findings


def validate_margin(
    frame: pd.DataFrame, dataset: str, *, empty_warn_only: bool = False
) -> list[QualityFinding]:
    findings = check_empty(frame, dataset, warn_only=empty_warn_only)
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


def validate_bse_margin(frame: pd.DataFrame, dataset: str = "bse_margin") -> list[QualityFinding]:
    """Validate the BSE aggregate row without mixing it with SSE/SZSE data."""

    # BSE publishes its aggregate later than the other margin sources.  An
    # empty, schema-shaped response is therefore source-not-ready and must be
    # represented as pending; parser/date/market violations remain hard fails.
    findings = validate_margin(frame, dataset, empty_warn_only=True)
    findings += check_required_columns(frame, dataset, ["source_trade_date"])
    if not frame.empty and "market" in frame.columns:
        invalid_market = int((frame["market"].astype(str) != "BSE").sum())
        findings.append(
            _finding(
                "validity.bse_margin_market",
                dataset,
                invalid_market == 0,
                message="北交所两融市场标记正确" if invalid_market == 0 else "北交所两融混入其他市场",
                evidence={"invalid_rows": invalid_market},
                remediation="检查北交所汇总适配器，禁止把沪深数据归入 BSE。",
            )
        )
    if not frame.empty and {"trade_date", "source_trade_date"}.issubset(frame.columns):
        mismatched = int(
            frame["trade_date"].astype(str).ne(frame["source_trade_date"].astype(str)).sum()
        )
        findings.append(
            _finding(
                "timeliness.bse_source_date",
                dataset,
                mismatched == 0,
                message="北交所源站日期与交易日一致" if mismatched == 0 else "北交所源站日期不一致",
                evidence={"mismatched_rows": mismatched},
                remediation="保留 JSONP 返回日期；日期不一致时阻断归档。",
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
