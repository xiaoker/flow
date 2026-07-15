from __future__ import annotations

import json
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook


ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = ROOT / "reports/phase0/latest-quality-summary.json"
MANIFEST_PATH = ROOT / "reports/phase0/latest-manifest.json"
OUTPUT_PATH = ROOT / "notebooks/phase0_data_quality.ipynb"


def main() -> None:
    quality = json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    counts = quality["counts"]
    dates = "、".join(manifest["requested_dates"])
    gate = quality["phase0_gate_status"]

    cells = [
        new_markdown_cell("# 交易台 Phase 0 数据质量验证"),
        new_markdown_cell(
            "## tl;dr\n\n"
            f"- 本次验证覆盖 {dates} 三个交易日，结果为 `{gate}`。\n"
            f"- 自动化质量规则通过 {counts['pass']} 项、警告 {counts['warn']} 项、失败 {counts['fail']} 项。\n"
            "- 上交所 ETF、沪深两融和港交所互联互通均完成三日验证；深交所 ETF 只有无统计日期的当前快照，因此只能从正式运行日起向前积累。"
        ),
        new_markdown_cell(
            "## Context & Methods\n\n"
            "目标是判断官方公开源是否足以支撑个人日频资金流交易台。粒度分别是 ETF×交易日、市场×交易日，以及互联互通通道×交易日。"
            "所有金额归一到基础货币单位，所有数量归一到股/份。原始响应与 SHA-256 哈希由采集器保存。\n\n"
            "### Key Assumptions\n\n"
            "- 港交所北向在 2024-08-19 后不再披露买卖额，因此北向净流入必须为空。\n"
            "- 深交所两融页面单位为亿元/亿股，标准化时乘以 1e8。\n"
            "- 深交所 ETF 当前下载没有源站统计日期，采集日期只能作为推断日期并产生高等级警告。"
        ),
        new_code_cell(
            "from pathlib import Path\n"
            "import json\n"
            "import pandas as pd\n\n"
            "ROOT = Path.cwd()\n"
            "quality = json.loads((ROOT / 'reports/phase0/latest-quality-summary.json').read_text(encoding='utf-8'))\n"
            "manifest = json.loads((ROOT / 'reports/phase0/latest-manifest.json').read_text(encoding='utf-8'))\n\n"
            "def load_dataset(name: str) -> pd.DataFrame:\n"
            "    return pd.read_csv(ROOT / manifest['combined_paths'][name])\n\n"
            "quality['phase0_gate_status'], manifest['run_id']"
        ),
        new_markdown_cell("## Data\n\n本节检查每个规范化数据集的行数、日期覆盖和缺失模式。"),
        new_code_cell(
            "profile_rows = []\n"
            "for dataset, item in quality['profiles'].items():\n"
            "    profile_rows.append({\n"
            "        'dataset': dataset,\n"
            "        'rows': item['rows'],\n"
            "        'dates': ', '.join(item['distinct_trade_dates']),\n"
            "        'date_count': len(item['distinct_trade_dates']),\n"
            "    })\n"
            "pd.DataFrame(profile_rows).sort_values('dataset').reset_index(drop=True)"
        ),
        new_markdown_cell("## Results\n\n质量门槛没有失败项；需要人工理解的证据集中在深交所 ETF 日期语义。"),
        new_code_cell(
            "non_pass = pd.DataFrame([item for item in quality['findings'] if item['status'] != 'pass'])\n"
            "non_pass[['dataset', 'status', 'severity', 'message', 'remediation']]"
        ),
        new_markdown_cell("### 两融单位与跨市场汇总"),
        new_code_cell(
            "margin = pd.concat([load_dataset('sse_margin'), load_dataset('szse_margin')], ignore_index=True)\n"
            "margin_daily = margin.groupby('trade_date', as_index=False).agg(\n"
            "    financing_balance_yuan=('financing_balance_yuan', 'sum'),\n"
            "    financing_buy_yuan=('financing_buy_yuan', 'sum'),\n"
            "    securities_lending_balance_yuan=('securities_lending_balance_yuan', 'sum'),\n"
            "    margin_total_balance_yuan=('margin_total_balance_yuan', 'sum'),\n"
            ")\n"
            "margin_daily['financing_balance_billion_yuan'] = margin_daily['financing_balance_yuan'] / 1e9\n"
            "margin_daily['financing_balance_change_billion_yuan'] = margin_daily['financing_balance_yuan'].diff() / 1e9\n"
            "margin_daily[['trade_date', 'financing_balance_billion_yuan', 'financing_balance_change_billion_yuan']]"
        ),
        new_markdown_cell("### 互联互通只使用仍被官方披露的口径"),
        new_code_cell(
            "connect = load_dataset('stock_connect_summary')\n"
            "south = connect[connect['direction'] == 'southbound'].groupby('trade_date', as_index=False)['net_buy'].sum()\n"
            "south['southbound_net_buy_billion_hkd'] = south['net_buy'] / 1e9\n"
            "north = connect[connect['direction'] == 'northbound'].groupby('trade_date', as_index=False)['total_turnover'].sum()\n"
            "north['northbound_turnover_billion_rmb'] = north['total_turnover'] / 1e9\n"
            "south[['trade_date', 'southbound_net_buy_billion_hkd']].merge(\n"
            "    north[['trade_date', 'northbound_turnover_billion_rmb']], on='trade_date'\n"
            ")"
        ),
        new_markdown_cell("### ETF 日期覆盖"),
        new_code_cell(
            "sse_etf = load_dataset('sse_etf')\n"
            "szse_etf = load_dataset('szse_etf')\n"
            "pd.concat([\n"
            "    sse_etf.groupby(['market', 'trade_date'], as_index=False).agg(rows=('instrument_code', 'size'), total_shares=('shares', 'sum')),\n"
            "    szse_etf.groupby(['market', 'trade_date'], as_index=False).agg(rows=('instrument_code', 'size'), total_shares=('shares', 'sum')),\n"
            "], ignore_index=True)"
        ),
        new_markdown_cell(
            "## Takeaways\n\n"
            "1. 可以进入 Phase 1 的接口和数据模型开发：上交所 ETF、沪深两融和港交所互联互通均具备可重放的官方数据链路。\n"
            "2. 深交所 ETF 历史回填不能依赖当前官方接口；上线后必须每天保存快照，并在累计三个真实快照前保持 `conditional_pass`。\n"
            "3. 只有三个观测日，不足以绘制有解释力的趋势图；本 Notebook 保留精确表格，趋势分析留到 20 个交易日后。"
        ),
    ]
    notebook = new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
        },
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, OUTPUT_PATH)
    executed = NotebookClient(
        notebook,
        timeout=120,
        kernel_name="python3",
        resources={"metadata": {"path": str(ROOT)}},
    ).execute()
    nbformat.write(executed, OUTPUT_PATH)
    print(f"Executed {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

