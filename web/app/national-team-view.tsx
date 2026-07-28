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

// This derived observation pool stores codes only. Every displayed name and
// value must come from the matching official ETF Radar row.
const nationalTeamEtfCodes = [
  "510300",
  "510310",
  "510330",
  "510500",
  "159915",
  "159919",
  "512100",
  "510050",
  "588080",
  "510180",
  "159845",
  "512500",
] as const;

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
  instrument: EtfTerminalInstrument | null;
};

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
    return nationalTeamEtfCodes.map((code) => ({
      code,
      instrument: byCode.get(code) ?? null,
    }));
  }, [terminal.instruments]);
  const [selectedCode, setSelectedCode] = useState(nationalTeamEtfCodes[0]);
  const selected = rows.find((item) => item.code === selectedCode) ?? rows[0];
  const normalizedQuery = query.trim().toLowerCase();
  const filteredRows = rows.filter(
    (item) =>
      !normalizedQuery ||
      item.code.includes(normalizedQuery) ||
      item.instrument?.name.toLowerCase().includes(normalizedQuery),
  );
  const matchedRows = rows.filter((item) => item.instrument);
  const rowsWithShares = matchedRows.filter(
    (item) => item.instrument?.shares !== null && item.instrument?.shares !== undefined,
  );
  const totalShares = rowsWithShares.reduce(
    (sum, item) => sum + (item.instrument?.shares ?? 0),
    0,
  );
  const periodFlow = matchedRows.reduce(
    (sum, item) => sum + (item.instrument?.periods[period] ?? 0),
    0,
  );

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
  const chartOption = {
    animationDuration: 420,
    grid: { left: 72, right: 34, top: 28, bottom: 46 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: "#728197", type: "dashed" } },
      backgroundColor: "#111923",
      borderColor: "#334155",
      textStyle: { color: "#e7edf5", fontSize: 14 },
      valueFormatter: (value: number) => `${Number(value).toFixed(2)} 亿份`,
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
      name: "亿份",
      nameTextStyle: { color: "#7f8da0", padding: [0, 0, 8, 0] },
      splitLine: { lineStyle: { color: "rgba(68,82,102,.32)", type: "dashed" } },
      axisLabel: {
        color: "#8795a8",
        fontFamily: "monospace",
        fontSize: 13,
      },
    },
    series: [
      {
        name: "官方总份额",
        type: "line",
        smooth: 0.16,
        showSymbol: true,
        symbolSize: 8,
        data: shareHistory.map((item) => item.value),
        lineStyle: { width: 3, color: "#e14f45" },
        itemStyle: { color: "#f2b24a", borderColor: "#132234", borderWidth: 2 },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(225,79,69,.28)" },
              { offset: 1, color: "rgba(225,79,69,.015)" },
            ],
          },
        },
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
            <strong>{selectedInstrument?.name ?? "原始 ETF 数据未匹配"} · 官方总份额</strong>
            <span>{selected.code} · 周期锚点复算 · 截至 {asOf}</span>
          </div>
          <span className="disclosure-pending">
            <Info size={15} />持有人披露待接入
          </span>
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
          历史节点＝当前官方总份额－对应周期份额变化；这是 ETF 总份额曲线，不代表国家队持仓变化。
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
                  <th>ETF 名称</th>
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
                          <strong>{instrument?.name ?? "原始数据未匹配"}</strong>
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
            <h2>{selected.instrument?.name ?? "原始 ETF 数据未匹配"}</h2>
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
