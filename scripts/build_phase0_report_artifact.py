from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports/phase0"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dataset(manifest: dict[str, Any], name: str) -> pd.DataFrame:
    return pd.read_csv(ROOT / manifest["combined_paths"][name])


def source_specs(generated_at: str, queries: dict[str, str]) -> list[dict[str, Any]]:
    definitions = {
        "headline-sql": [
            "通过项：status=pass 的质量规则数量",
            "警告项：不阻断规范化数据使用但需要显式约束的规则数量",
            "三日覆盖链路：请求的三个交易日均存在数据的核心数据集数量",
        ],
        "daily-sql": [
            "沪深北融资余额：上交所、深交所与北交所融资余额之和，单位人民币十亿元",
            "南向净买入：港股通沪与港股通深买入成交额减卖出成交额之和，单位港币十亿元",
            "北向成交额：沪股通与深股通总成交额之和，单位人民币十亿元；不是净流入",
        ],
    }
    metadata = {
        "headline-sql": ("报告核心质量指标", "headline.sql", ["quality_counts", "dataset_profiles"]),
        "coverage-sql": ("数据集日期覆盖", "coverage.sql", ["dataset_profiles"]),
        "warnings-sql": ("非通过质量发现", "warnings.sql", ["quality_findings"]),
        "unit-sql": ("两融单位一致性检查", "unit_checks.sql", ["quality_findings"]),
        "daily-sql": (
            "三日市场级规范化证据",
            "daily_market.sql",
            ["normalized_margin", "normalized_connect_summary"],
        ),
    }
    sources = []
    for source_id, (label, filename, tables) in metadata.items():
        sources.append(
            {
                "id": source_id,
                "label": label,
                "path": f"reports/phase0/sql/{filename}",
                "query": {
                    "sql": queries[source_id],
                    "description": label,
                    "engine": "SQLite",
                    "language": "sql",
                    "executed_at": generated_at,
                    "tables_used": tables,
                    "filters": ["仅本次 Phase 0 run 的三个请求交易日"],
                    "metric_definitions": definitions.get(source_id, []),
                },
            }
        )
    return sources


def main() -> None:
    quality = load_json(REPORT_DIR / "latest-quality-summary.json")
    manifest = load_json(REPORT_DIR / "latest-manifest.json")
    generated_at = manifest["finished_at"]
    counts = quality["counts"]

    dataset_labels = {
        "sse_etf": "上交所 ETF",
        "szse_etf": "深交所 ETF",
        "sse_margin": "上交所两融",
        "szse_margin": "深交所两融",
        "bse_margin": "北交所两融",
        "stock_connect_summary": "互联互通汇总",
        "stock_connect_top": "互联互通前十",
    }
    coverage_rows = []
    for dataset, item in quality["profiles"].items():
        dates = item["distinct_trade_dates"]
        expected = manifest["requested_dates"]
        complete = set(expected).issubset(set(dates))
        coverage_rows.append(
            {
                "dataset": dataset,
                "dataset_label": dataset_labels.get(dataset, dataset),
                "rows": item["rows"],
                "date_count": len(dates),
                "target_date_count": len(expected),
                "coverage": "完整" if complete else "受限",
                "dates": "、".join(dates),
            }
        )

    warning_rows = [
        {
            "dataset": item["dataset"],
            "severity": item["severity"],
            "message": item["message"],
            "remediation": item["remediation"],
        }
        for item in quality["findings"]
        if item["status"] != "pass"
    ]

    margin = pd.concat(
        [
            load_dataset(manifest, "sse_margin"),
            load_dataset(manifest, "szse_margin"),
            load_dataset(manifest, "bse_margin"),
        ],
        ignore_index=True,
    )
    connect = load_dataset(manifest, "stock_connect_summary")
    findings_rows = []
    for item in quality["findings"]:
        evidence = item.get("evidence") or {}
        findings_rows.append(
            {
                "check_id": item["check_id"],
                "dataset": item["dataset"],
                "status": item["status"],
                "severity": item["severity"],
                "message": item["message"],
                "remediation": item.get("remediation"),
                "max_abs_delta_yuan": evidence.get("max_abs_delta_yuan"),
                "tolerance_yuan": evidence.get("tolerance_yuan"),
            }
        )
    sql_dir = REPORT_DIR / "sql"
    query_files = {
        "headline-sql": "headline.sql",
        "coverage-sql": "coverage.sql",
        "warnings-sql": "warnings.sql",
        "unit-sql": "unit_checks.sql",
        "daily-sql": "daily_market.sql",
    }
    queries = {
        source_id: (sql_dir / filename).read_text(encoding="utf-8")
        for source_id, filename in query_files.items()
    }
    with sqlite3.connect(":memory:") as connection:
        pd.DataFrame(
            [
                {
                    "pass_count": counts["pass"],
                    "warn_count": counts["warn"],
                    "fail_count": counts["fail"],
                }
            ]
        ).to_sql("quality_counts", connection, index=False)
        pd.DataFrame(coverage_rows).to_sql("dataset_profiles", connection, index=False)
        pd.DataFrame(findings_rows).to_sql("quality_findings", connection, index=False)
        margin.to_sql("normalized_margin", connection, index=False)
        connect.to_sql("normalized_connect_summary", connection, index=False)
        headline = pd.read_sql_query(queries["headline-sql"], connection).to_dict("records")
        coverage_rows = pd.read_sql_query(queries["coverage-sql"], connection).to_dict("records")
        warning_rows = pd.read_sql_query(queries["warnings-sql"], connection).to_dict("records")
        unit_rows = pd.read_sql_query(queries["unit-sql"], connection).to_dict("records")
        daily_rows = pd.read_sql_query(queries["daily-sql"], connection).to_dict("records")
    sources = source_specs(generated_at, queries)
    title = "交易台 Phase 0 数据可行性报告"
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "官方 ETF、两融和互联互通数据的三交易日可用性与质量结论",
            "generatedAt": generated_at,
            "sources": sources,
            "cards": [
                {
                    "id": "passed-card",
                    "dataset": "headline",
                    "sourceId": "headline-sql",
                    "description": "完成的自动化数据质量规则",
                    "metrics": [{"label": "通过检查", "field": "passed_checks", "format": "number"}],
                },
                {
                    "id": "failed-card",
                    "dataset": "headline",
                    "sourceId": "headline-sql",
                    "description": "会阻断 Phase 0 的失败规则",
                    "metrics": [{"label": "失败检查", "field": "failed_checks", "format": "number"}],
                },
                {
                    "id": "coverage-card",
                    "dataset": "headline",
                    "sourceId": "headline-sql",
                    "description": "官方源可覆盖全部三个请求交易日的核心链路",
                    "metrics": [
                        {"label": "三日完整链路", "field": "complete_three_day_chains", "format": "number"},
                        {"label": "高等级警告", "field": "warning_checks", "format": "number"},
                    ],
                },
            ],
            "charts": [
                {
                    "id": "coverage-chart",
                    "title": "数据链路交易日覆盖",
                    "subtitle": "实际可用日期数；参考线为本次验证目标 3 个交易日",
                    "showDescription": True,
                    "intent": "comparison",
                    "question": "哪些官方数据链路覆盖了全部三个请求交易日？",
                    "rationale": "六条链路是离散类别，横向条形图便于比较长标签，并用目标参考线突出唯一缺口。",
                    "comparisonContext": {
                        "baseline": "3 个请求交易日",
                        "grain": "数据链路",
                        "unit": "交易日数",
                    },
                    "type": "horizontalBar",
                    "dataset": "coverage",
                    "sourceId": "coverage-sql",
                    "encodings": {
                        "x": {
                            "field": "dataset_label",
                            "type": "nominal",
                            "label": "数据链路",
                        },
                        "y": {
                            "field": "date_count",
                            "type": "quantitative",
                            "label": "实际覆盖",
                            "unit": "交易日",
                        },
                        "tooltip": [
                            {"field": "rows", "type": "quantitative", "label": "行数"},
                            {"field": "dates", "type": "text", "label": "可用日期"},
                            {"field": "coverage", "type": "text", "label": "覆盖状态"},
                        ],
                    },
                    "yAxisTitle": "实际覆盖交易日数",
                    "valueFormat": "number",
                    "unit": "个交易日",
                    "referenceLines": [
                        {
                            "axis": "y",
                            "value": 3,
                            "label": "目标 3 天",
                            "color": "neutral",
                            "lineStyle": "dashed",
                        }
                    ],
                    "labels": {"values": "all"},
                    "maxRows": 6,
                    "emptyState": "没有可用于覆盖比较的数据",
                }
            ],
            "tables": [
                {
                    "id": "coverage-table",
                    "title": "数据链路覆盖",
                    "subtitle": "本次请求的三个交易日；深交所 ETF 为单一当前快照",
                    "showDescription": True,
                    "dataset": "coverage",
                    "defaultSort": {"field": "dataset", "direction": "asc"},
                    "density": "spacious",
                    "sourceId": "coverage-sql",
                    "columns": [
                        {"field": "dataset", "label": "数据集", "type": "text"},
                        {"field": "rows", "label": "行数", "format": "number"},
                        {"field": "date_count", "label": "日期数", "format": "number"},
                        {"field": "coverage", "label": "覆盖状态", "type": "text"},
                        {"field": "dates", "label": "可用日期", "type": "text"},
                    ],
                },
                {
                    "id": "daily-table",
                    "title": "三日市场级验证值",
                    "subtitle": "两融为人民币十亿元；南向净买入为港币十亿元；北向仅为人民币成交额",
                    "showDescription": True,
                    "dataset": "daily",
                    "defaultSort": {"field": "trade_date", "direction": "asc"},
                    "density": "spacious",
                    "sourceId": "daily-sql",
                    "columns": [
                        {"field": "trade_date", "label": "交易日", "type": "date"},
                        {"field": "financing_balance_billion_yuan", "label": "沪深北融资余额（十亿元）", "format": "number"},
                        {"field": "southbound_net_buy_billion_hkd", "label": "南向净买入（十亿港元）", "format": "number", "movement": True},
                        {"field": "northbound_turnover_billion_rmb", "label": "北向成交额（十亿元）", "format": "number"},
                    ],
                },
                {
                    "id": "warning-table",
                    "title": "剩余质量约束",
                    "subtitle": "均来自深交所 ETF 当前快照的日期语义和历史覆盖限制",
                    "showDescription": True,
                    "dataset": "warnings",
                    "defaultSort": {"field": "severity", "direction": "asc"},
                    "density": "spacious",
                    "sourceId": "warnings-sql",
                    "columns": [
                        {"field": "dataset", "label": "数据集", "type": "text"},
                        {"field": "severity", "label": "等级", "type": "text"},
                        {"field": "message", "label": "发现", "type": "text"},
                        {"field": "remediation", "label": "处理", "type": "text"},
                    ],
                },
                {
                    "id": "unit-table",
                    "title": "两融单位一致性",
                    "subtitle": "总余额与融资余额加融券余额的绝对差；深交所允许亿元展示舍入",
                    "showDescription": True,
                    "dataset": "unit_checks",
                    "defaultSort": {"field": "dataset", "direction": "asc"},
                    "density": "spacious",
                    "sourceId": "unit-sql",
                    "columns": [
                        {"field": "dataset", "label": "数据集", "type": "text"},
                        {"field": "status", "label": "结果", "type": "text"},
                        {"field": "max_abs_delta_yuan", "label": "最大差额（元）", "format": "number"},
                        {"field": "tolerance_yuan", "label": "容差（元）", "format": "number"},
                    ],
                },
            ],
            "blocks": [
                {"id": "title", "type": "markdown", "layout": "full", "body": f"# {title}"},
                {
                    "id": "summary",
                    "type": "markdown",
                    "layout": "full",
                    "sourceId": "headline-sql",
                    "body": (
                        "## 技术结论：可进入 Phase 1，但深交所 ETF 需带约束\n\n"
                        f"本次三交易日验证得到 **{counts['pass']} 项通过、{counts['fail']} 项失败、{counts['warn']} 项警告**。"
                        "上交所 ETF、沪深两融和港交所互联互通已形成可重放的官方数据链路。"
                        "深交所 ETF 官方下载只有无统计日期的当前快照，因此结论是 `conditional_pass`：可以继续开发，但不得宣称可官方回填其历史份额。"
                    ),
                },
                {"id": "metrics", "type": "metric-strip", "layout": "full", "cardIds": ["passed-card", "failed-card", "coverage-card"]},
                {
                    "id": "coverage-heading",
                    "type": "markdown",
                    "layout": "full",
                    "body": "## 四条核心链路完成三日覆盖，深交所 ETF 例外\n\n覆盖表按数据集列出实际日期。这个限制影响历史 ETF 申赎计算，但不影响从正式定时采集日起向前建立份额序列。",
                },
                {"id": "coverage-visual", "type": "chart", "layout": "full", "chartId": "coverage-chart"},
                {"id": "coverage", "type": "table", "layout": "full", "tableId": "coverage-table"},
                {
                    "id": "values-heading",
                    "type": "markdown",
                    "layout": "full",
                    "body": "## 市场级数值通过单位和披露口径校验\n\n表中的北向字段是官方总成交额，不是净流入；南向净买入由两个南向通道的买入减卖出得到。三天样本用于接口和单位验证，不足以解释趋势。",
                },
                {"id": "daily", "type": "table", "layout": "full", "tableId": "daily-table"},
                {
                    "id": "definitions",
                    "type": "markdown",
                    "layout": "full",
                    "body": (
                        "## 口径和方法已固定为可审计契约\n\n"
                        "上交所两融源字段是元和股；深交所页面明确以亿元和亿股/亿份展示，标准化时乘以 `1e8`。"
                        "港交所摘要金额以百万元展示，标准化时乘以 `1e6`。北向买卖额和净流入在当前披露机制下保持空值。"
                    ),
                },
                {"id": "unit", "type": "table", "layout": "full", "tableId": "unit-table"},
                {
                    "id": "limitations",
                    "type": "markdown",
                    "layout": "full",
                    "body": (
                        "## 当前存在一个数据源限制和一个云端验证缺口\n\n"
                        "只有三个交易日，尚不能评价月度稳定性、节假日边界或长期字段漂移，因此没有绘制趋势图。"
                        "深交所 ETF 的统计日期由采集任务推断；正式系统必须保存每日原始 XLSX、哈希和观察时间，并在积累三个真实定时快照前持续显示质量警告。"
                        "本轮也没有配置 GitHub/Cloudflare 资源和密钥，因此 R2/D1 写入链路尚未实测，不计入已完成验收项。"
                    ),
                },
                {"id": "warnings", "type": "table", "layout": "full", "tableId": "warning-table"},
                {
                    "id": "next",
                    "type": "markdown",
                    "layout": "full",
                    "body": (
                        "## 建议下一步：开始 Phase 1 骨架，同时连续观察深交所快照\n\n"
                        "- 建立 D1 schema、数据域发布状态机和只读 Worker API。\n"
                        "- 创建临时 D1/R2 资源并配置 GitHub Environment secrets，先完成一次可回滚的云端写入冒烟。\n"
                        "- 再把当前采集命令放入 GitHub Actions，连续保存深交所 ETF 快照。\n"
                        "- 累计 20 个交易日后再评估趋势、分布漂移和异常阈值。"
                    ),
                },
                {
                    "id": "questions",
                    "type": "markdown",
                    "layout": "full",
                    "body": (
                        "## 后续需要继续验证的问题\n\n"
                        "- 深交所 ETF XLSX 是否在交易日夜间固定时间更新？\n"
                        "- ETF 拆分、折算和新上市产品在两个交易所接口中如何表达？\n"
                        "- GitHub Actions 的云端出口是否会触发交易所限流，是否需要错峰或代理降级？"
                    ),
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "headline": headline,
                "coverage": coverage_rows,
                "warnings": warning_rows,
                "daily": daily_rows,
                "unit_checks": unit_rows,
            },
        },
        "sources": sources,
    }
    output_path = REPORT_DIR / "artifact.json"
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
