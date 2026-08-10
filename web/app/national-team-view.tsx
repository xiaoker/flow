"use client";

import {
  Buildings,
  CaretRight,
  Database,
  Info,
  ShieldCheck,
} from "@phosphor-icons/react";
import ReactECharts from "echarts-for-react";
import { useMemo, useState } from "react";
import type {
  EtfTerminalData,
  EtfTerminalInstrument,
  FlowPeriod,
} from "./lib/dashboard-data";

// The observation pool is still a code-based subset of ETF Radar. Fund short
// names are display metadata; every share and period value continues to come
// from the matching official ETF Radar row.
export const nationalTeamEtfs = [
  { code: "510300", fundShortName: "沪深300ETF华泰柏瑞" },
  { code: "510310", fundShortName: "沪深300ETF易方达" },
  { code: "510330", fundShortName: "沪深300ETF华夏" },
  { code: "510500", fundShortName: "中证500ETF南方" },
  { code: "159915", fundShortName: "创业板ETF易方达" },
  { code: "159919", fundShortName: "沪深300ETF嘉实" },
  { code: "512100", fundShortName: "中证1000ETF南方" },
  { code: "510050", fundShortName: "上证50ETF华夏" },
  { code: "588080", fundShortName: "科创50ETF易方达" },
  { code: "510180", fundShortName: "上证180ETF华安" },
  { code: "159845", fundShortName: "中证1000ETF华夏" },
  { code: "512500", fundShortName: "中证500ETF华夏" },
] as const;

/**
 * The national-team page is a code-based subset of ETF Radar.  Period
 * availability must therefore be derived from the matching official rows,
 * rather than trusting the top-level terminal metadata (which can be
 * conservative while a release is being rebuilt).
 */
export function getNationalAvailablePeriods(terminal: EtfTerminalData): Set<FlowPeriod> {
  const codes = new Set(nationalTeamEtfs.map((item) => item.code));
  return new Set(
    (Object.keys(periodLabels) as FlowPeriod[]).filter((period) =>
      terminal.instruments.some(
        (item) => codes.has(item.code) && typeof item.periods[period] === "number",
      ),
    ),
  );
}

const periodLabels: Record<FlowPeriod, string> = {
  "1d": "最近1日",
  "3d": "最近3日",
  "1w": "1周",
  "2w": "2周",
  "1m": "1月",
  "3m": "3月",
  "6m": "6月",
  ytd: "今年来",
  "12m": "12月",
};

const historyPeriods: Array<{ id: FlowPeriod; label: string }> = [
  { id: "12m", label: "12月前" },
  { id: "ytd", label: "年初" },
  { id: "6m", label: "6月前" },
  { id: "3m", label: "3月前" },
  { id: "1m", label: "1月前" },
  { id: "2w", label: "2周前" },
  { id: "1w", label: "1周前" },
  { id: "3d", label: "3日前" },
  { id: "1d", label: "前一日" },
];

type NationalTeamRow = {
  code: string;
  fundShortName: string;
  instrument: EtfTerminalInstrument | null;
};

type NationalChartMode = "relative" | "absolute";

function signed(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function flowClass(value: number | null | undefined) {
  if (value === null || value === undefined || value === 0) return "";
  return value > 0 ? "text-in" : "text-out";
}

export function NationalTeamView({
  terminal,
  asOf,
  period,
  query,
}: {
  terminal: EtfTerminalData;
  asOf: string;
  period: FlowPeriod;
  query: string;
}) {
  const rows = useMemo<NationalTeamRow[]>(() => {
    const byCode = new Map(terminal.instruments.map((item) => [item.code, item]));
    return nationalTeamEtfs.map(({ code, fundShortName }) => ({
      code,
      fundShortName,
      instrument: byCode.get(code) ?? null,
    }));
  }, [terminal.instruments]);
  const [selectedCode, setSelectedCode] = useState(nationalTeamEtfs[0].code);
  const [chartMode, setChartMode] = useState<NationalChartMode>("relative");
  const selected = rows.find((item) => item.code === selectedCode) ?? rows[0];
  const normalizedQuery = query.trim().toLowerCase();
  const filteredRows = rows.filter(
    (item) =>
      !normalizedQuery ||
      item.code.includes(normalizedQuery) ||
      item.fundShortName.toLowerCase().includes(normalizedQuery),
  );
  const matchedRows = rows.filter((item) => item.instrument);
  const rowsWithShares = matchedRows.filter(
    (item) => item.instrument?.shares !== null && item.instrument?.shares !== undefined,
  );
  const totalShares = rowsWithShares.reduce(
    (sum, item) => sum + (item.instrument?.shares ?? 0),
    0,
  );
  const periodValues = matchedRows
    .map((item) => item.instrument?.periods[period])
    .filter((value): value is number => typeof value === "number");
  const periodFlow = periodValues.length
    ? periodValues.reduce((sum, value) => sum + value, 0)
    : null;

  const selectedInstrument = selected.instrument;
  const shareHistory =
    typeof selectedInstrument?.shares === "number"
      ? [
          ...historyPeriods.flatMap(({ id, label }) => {
            const change = selectedInstrument.periods[id];
            return typeof change === "number"
              ? [{ label, value: selectedInstrument.shares! - change }]
              : [];
          }),
          { label: "当前", value: selectedInstrument.shares },
        ]
      : [];
  const chartBase = shareHistory[0]?.value ?? null;
  const chartPoints = shareHistory.map((item) => ({
    ...item,
    delta: chartBase === null ? null : item.value - chartBase,
    relative: chartBase && chartBase !== 0 ? ((item.value / chartBase) - 1) * 100 : null,
  }));
  const largestChartMove = chartPoints.reduce<{ label: string; delta: number; relative: number | null } | null>(
    (largest, item) => {
      if (item.delta === null) return largest;
      if (!largest || Math.abs(item.delta) > Math.abs(largest.delta)) {
        return { label: item.label, delta: item.delta, relative: item.relative };
      }
      return largest;
    },
    null,
  );
  const chartValues = chartMode === "relative"
    ? chartPoints.map((item) => item.relative)
    : chartPoints.map((item) => item.value);
  const chartUnit = chartMode === "relative" ? "%" : "亿份";
  const chartSeriesName = chartMode === "relative" ? "相对最早节点变化" : "官方总份额";
  const chartOption = {
    animationDuration: 420,
    grid: { left: chartMode === "relative" ? 68 : 72, right: 34, top: 28, bottom: 46 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: "#728197", type: "dashed" } },
      backgroundColor: "#111923",
      borderColor: "#334155",
      textStyle: { color: "#e7edf5", fontSize: 14 },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const point = chartPoints[params[0]?.dataIndex ?? 0];
        if (!point) return "";
        const relative = point.relative === null ? "—" : `${point.relative >= 0 ? "+" : ""}${point.relative.toFixed(2)}%`;
        const delta = point.delta === null ? "—" : `${point.delta >= 0 ? "+" : ""}${point.delta.toFixed(2)} 亿份`;
        return `${point.label}<br/>官方总份额：${point.value.toFixed(2)} 亿份<br/>相对最早节点：${relative}<br/>变化：${delta}`;
      },
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: shareHistory.map((item) => item.label),
      axisLine: { lineStyle: { color: "#394759" } },
      axisTick: { show: false },
      axisLabel: {
        color: "#8795a8",
        fontFamily: "monospace",
        fontSize: 13,
      },
    },
    yAxis: {
      type: "value",
      scale: true,
      name: chartUnit,
      nameTextStyle: { color: "#7f8da0", padding: [0, 0, 8, 0] },
      splitLine: { lineStyle: { color: "rgba(68,82,102,.32)", type: "dashed" } },
      axisLabel: {
        color: "#8795a8",
        fontFamily: "monospace",
        fontSize: 13,
        formatter: (value: number) => chartMode === "relative" ? `${value > 0 ? "+" : ""}${value.toFixed(0)}%` : value.toFixed(0),
      },
    },
    series: [
      {
        name: chartSeriesName,
        type: "line",
        smooth: 0.16,
        showSymbol: true,
        symbolSize: (value: number, params: { dataIndex: number }) => params.dataIndex === chartPoints.length - 1 ? 11 : 7,
        data: chartValues,
        lineStyle: { width: 3, color: chartMode === "relative" ? "#50b7d9" : "#e14f45" },
        itemStyle: { color: "#f2b24a", borderColor: "#132234", borderWidth: 2 },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: chartMode === "relative" ? "rgba(80,183,217,.26)" : "rgba(225,79,69,.28)" },
              { offset: 1, color: chartMode === "relative" ? "rgba(80,183,217,.015)" : "rgba(225,79,69,.015)" },
            ],
          },
        },
        markLine: chartMode === "relative" ? {
          silent: true,
          symbol: "none",
          lineStyle: { color: "rgba(241,174,66,.75)", type: "dashed", width: 1 },
          label: { color: "#f1ae42", formatter: "起点 0%" },
          data: [{ yAxis: 0 }],
        } : undefined,
      },
    ],
  };

  return (
    <section className="national-page">
      <article className="terminal-panel national-hero">
        <div className="national-heading">
          <div>
            <span className="eyebrow">国家队 ETF 观察池 · 官方份额</span>
            <h2>12 只重点 ETF</h2>
            <p>
              当前先展示交易所份额与变化；持有人披露将在基金定期报告接入后补充。
            </p>
          </div>
          <div className="national-summary">
            <span>
              <small>观察池</small>
              <strong>12<em>只</em></strong>
            </span>
            <span>
              <small>官方匹配</small>
              <strong className="text-cyan">{matchedRows.length}<em>只</em></strong>
            </span>
            <span>
              <small>当前总份额</small>
              <strong>{rowsWithShares.length ? totalShares.toFixed(1) : "—"}<em>亿份</em></strong>
            </span>
            <span>
              <small>{periodLabels[period]}变化</small>
              <strong className={flowClass(periodFlow)}>
                {signed(periodFlow, 1)}<em>亿份</em>
              </strong>
            </span>
          </div>
        </div>
        <div className="national-chart-title">
          <div>
            <strong>{selected.fundShortName} · {chartMode === "relative" ? "份额变化" : "官方总份额"}</strong>
            <span>{selected.code} · {chartMode === "relative" ? "相对最早可比节点" : "周期锚点复算"} · 截至 {asOf}</span>
          </div>
          <div className="national-chart-tools">
            <div className="national-chart-mode" aria-label="图表显示方式" role="tablist">
              <button aria-selected={chartMode === "relative"} className={chartMode === "relative" ? "active" : ""} onClick={() => setChartMode("relative")} role="tab" type="button">变化%</button>
              <button aria-selected={chartMode === "absolute"} className={chartMode === "absolute" ? "active" : ""} onClick={() => setChartMode("absolute")} role="tab" type="button">绝对份额</button>
            </div>
            {largestChartMove && <span className={`national-chart-callout ${flowClass(largestChartMove.delta)}`}><small>最大变化 · {largestChartMove.label}</small><strong>{signed(largestChartMove.delta)}<em>亿份</em></strong>{largestChartMove.relative !== null && <b>{largestChartMove.relative >= 0 ? "+" : ""}{largestChartMove.relative.toFixed(1)}%</b>}</span>}
            <span className="disclosure-pending">
              <Info size={15} />持有人披露待接入
            </span>
          </div>
        </div>
        {shareHistory.length >= 2 ? (
          <ReactECharts
            className="national-chart"
            lazyUpdate
            notMerge
            option={chartOption}
          />
        ) : (
          <div className="national-chart national-chart-empty">
            <Database size={28} />
            <strong>等待该 ETF 的连续官方份额快照</strong>
            <span>形成至少两个可比周期节点后自动生成折线</span>
          </div>
        )}
        <p className="national-chart-footnote">
          {chartMode === "relative" ? "变化%＝（节点总份额 ÷ 最早可比节点总份额 − 1）× 100%；悬停可查看官方总份额。" : "历史节点＝当前官方总份额－对应周期份额变化；"}这是 ETF 总份额曲线，不代表国家队持仓变化。
        </p>
      </article>

      <section className="national-workspace">
        <article className="terminal-panel national-table-panel">
          <div className="national-table-heading">
            <div>
              <span className="eyebrow">ETF 观察明细</span>
              <h3>官方交易所快照</h3>
            </div>
            <span>{filteredRows.length} / 12 只</span>
          </div>
          <div className="national-table-wrap">
            <table className="national-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>基金简称</th>
                  <th>代码</th>
                  <th>当前份额</th>
                  <th>最近1日</th>
                  <th>1周</th>
                  <th>1月</th>
                  <th>披露状态</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row, index) => {
                  const instrument = row.instrument;
                  return (
                    <tr
                      className={selected.code === row.code ? "selected" : ""}
                      key={row.code}
                      onClick={() => setSelectedCode(row.code)}
                    >
                      <td>{String(index + 1).padStart(2, "0")}</td>
                      <td>
                        <button type="button" onClick={() => setSelectedCode(row.code)}>
                          <strong>{row.fundShortName}</strong>
                          <small>{instrument?.category ?? "待匹配分类"}</small>
                          <CaretRight size={14} />
                        </button>
                      </td>
                      <td>{row.code}</td>
                      <td>{instrument?.shares?.toFixed(1) ?? "—"}<small>亿份</small></td>
                      {(["1d", "1w", "1m"] as FlowPeriod[]).map((item) => (
                        <td className={flowClass(instrument?.periods[item])} key={item}>
                          {signed(instrument?.periods[item])}
                        </td>
                      ))}
                      <td><span className="pending-tag">待接入</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </article>

        <aside className="terminal-panel national-detail">
          <div className="national-detail-header">
            <span className="eyebrow">选中 ETF</span>
            <h2>{selected.fundShortName}</h2>
            <p>{selected.code} · {selected.instrument?.market === "SZSE" ? "深交所" : "上交所"}</p>
          </div>
          <div className="national-detail-metrics">
            <span>
              <small>当前官方份额</small>
              <strong>{selected.instrument?.shares?.toFixed(1) ?? "—"}<em>亿份</em></strong>
            </span>
            <span>
              <small>{periodLabels[period]}变化</small>
              <strong className={flowClass(selected.instrument?.periods[period])}>
                {signed(selected.instrument?.periods[period])}<em>亿份</em>
              </strong>
            </span>
          </div>
          <div className="national-disclosure-state">
            <ShieldCheck size={23} />
            <div>
              <strong>正式持有人披露尚未接入</strong>
              <p>
                不使用截图数字或第三方估算替代基金定期报告。报告期、持有比例和持有市值保持为空。
              </p>
            </div>
          </div>
          <div className="national-holder-checklist">
            <strong><Buildings size={18} />拟核对披露主体</strong>
            <div>
              <span>中央汇金资产管理有限责任公司</span>
              <small>待基金报告核验</small>
            </div>
            <div>
              <span>中央汇金投资有限责任公司</span>
              <small>待基金报告核验</small>
            </div>
          </div>
          <div className="national-method">
            <span>当前计算口径</span>
            <p>
              ETF 份额变化＝当日官方总份额－前一交易日官方总份额。它不等同于国家队增减持，也不等同资金净流入。
            </p>
          </div>
          {!selected.instrument && (
            <div className="national-missing">
              <Database size={18} />当前官方 ETF 快照尚未匹配该代码
            </div>
          )}
        </aside>
      </section>
    </section>
  );
}
