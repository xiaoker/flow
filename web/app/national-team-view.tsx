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

const nationalTeamEtfs = [
  { code: "510300", referenceName: "沪深300ETF 华泰柏瑞" },
  { code: "510310", referenceName: "沪深300ETF 易方达" },
  { code: "510330", referenceName: "沪深300ETF 华夏" },
  { code: "510500", referenceName: "中证500ETF 南方" },
  { code: "159915", referenceName: "创业板ETF 易方达" },
  { code: "159919", referenceName: "沪深300ETF 嘉实" },
  { code: "512100", referenceName: "中证1000ETF 南方" },
  { code: "510050", referenceName: "上证50ETF 华夏" },
  { code: "588080", referenceName: "科创50ETF 易方达" },
  { code: "510180", referenceName: "上证180ETF 华安" },
  { code: "159845", referenceName: "中证1000ETF 华夏" },
  { code: "512500", referenceName: "中证500ETF 华夏" },
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

type NationalTeamRow = {
  code: string;
  referenceName: string;
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
    return nationalTeamEtfs.map((item) => ({
      ...item,
      instrument: byCode.get(item.code) ?? null,
    }));
  }, [terminal.instruments]);
  const [selectedCode, setSelectedCode] = useState(nationalTeamEtfs[0].code);
  const selected = rows.find((item) => item.code === selectedCode) ?? rows[0];
  const normalizedQuery = query.trim().toLowerCase();
  const filteredRows = rows.filter(
    (item) =>
      !normalizedQuery ||
      item.code.includes(normalizedQuery) ||
      item.referenceName.toLowerCase().includes(normalizedQuery) ||
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

  const chartRows = [...matchedRows].sort(
    (left, right) =>
      Math.abs(right.instrument?.periods[period] ?? 0) -
      Math.abs(left.instrument?.periods[period] ?? 0),
  );
  const chartOption = {
    animationDuration: 420,
    grid: { left: 150, right: 80, top: 18, bottom: 26 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: "#111923",
      borderColor: "#334155",
      textStyle: { color: "#e7edf5", fontSize: 14 },
      valueFormatter: (value: number) => `${signed(Number(value))} 亿份`,
    },
    xAxis: {
      type: "value",
      axisLine: { lineStyle: { color: "#394759" } },
      splitLine: {
        lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" },
      },
      axisLabel: {
        color: "#8795a8",
        fontFamily: "monospace",
        fontSize: 13,
      },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: chartRows.map((item) => item.instrument?.name ?? item.referenceName),
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: {
        width: 126,
        overflow: "truncate",
        color: "#dce4ee",
        fontSize: 13,
        fontWeight: 600,
      },
    },
    series: [
      {
        type: "bar",
        barMaxWidth: 17,
        data: chartRows.map((item) => {
          const value = item.instrument?.periods[period] ?? 0;
          return {
            value,
            itemStyle: {
              color: value >= 0 ? "#e36464" : "#55b887",
              borderRadius: value >= 0 ? [0, 5, 5, 0] : [5, 0, 0, 5],
            },
          };
        }),
        label: {
          show: true,
          position: "right",
          color: "#bec8d5",
          fontFamily: "monospace",
          fontSize: 12,
          formatter: ({ value }: { value: number }) => signed(value),
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
            <strong>观察池 ETF 份额变化</strong>
            <span>{periodLabels[period]} · 截至 {asOf}</span>
          </div>
          <span className="disclosure-pending">
            <Info size={15} />持有人披露待接入
          </span>
        </div>
        <ReactECharts
          className="national-chart"
          lazyUpdate
          notMerge
          option={chartOption}
        />
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
                          <strong>{instrument?.name ?? row.referenceName}</strong>
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
            <h2>{selected.instrument?.name ?? selected.referenceName}</h2>
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
