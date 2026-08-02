"use client";

import {
  ArrowsClockwise,
  ArrowsLeftRight,
  Buildings,
  CalendarBlank,
  Camera,
  CaretDown,
  CaretLeft,
  CaretRight,
  ChartBar,
  ChartLineUp,
  Database,
  Info,
  MagnifyingGlass,
  Pulse,
  Table as TableIcon,
  X,
} from "@phosphor-icons/react";
import type { ColumnDef, SortingState } from "@tanstack/react-table";
import {
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  DashboardSnapshot,
  EvidenceKind,
  EtfTerminalData,
  EtfTerminalInstrument,
  FlowPeriod,
} from "./lib/dashboard-data";
import { NationalTeamView } from "./national-team-view";

type DashboardProps = {
  snapshot: DashboardSnapshot;
  availableDates?: string[];
  requestedDate?: string | null;
  exactDateMatch?: boolean;
  initialView?: DeskView;
};
type DeskView = "overview" | "radar" | "national" | "connect" | "margin";
type DirectionFilter = "all" | "inflow" | "outflow";
type MarketFilter = "all" | "SSE" | "SZSE";
type MarginDisplayMetric = "balance" | "financingBuy" | "lendingBalance";

const periods: Array<{ id: FlowPeriod; label: string }> = [
  { id: "1d", label: "最近1日" },
  { id: "3d", label: "最近3日" },
  { id: "1w", label: "1周" },
  { id: "2w", label: "2周" },
  { id: "1m", label: "1月" },
  { id: "3m", label: "3月" },
  { id: "6m", label: "6月" },
  { id: "ytd", label: "今年来" },
  { id: "12m", label: "12月" },
];

const shareHistoryPeriods: Array<{ id: FlowPeriod; label: string }> = [
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

const navigation: Array<{ id: DeskView; label: string; icon: typeof ChartBar }> = [
  { id: "overview", label: "数据总览", icon: ChartLineUp },
  { id: "radar", label: "ETF 雷达", icon: TableIcon },
  { id: "national", label: "国家队持仓", icon: Buildings },
  { id: "connect", label: "南北互通", icon: ArrowsLeftRight },
  { id: "margin", label: "融资融券", icon: Pulse },
];

const marginMetricDefinitions: Record<MarginDisplayMetric, { label: string; unit: string; note: string }> = {
  balance: { label: "融资余额", unit: "亿元", note: "沪深融资余额合计" },
  financingBuy: { label: "融资买入额", unit: "亿元", note: "当日新增融资买入" },
  lendingBalance: { label: "融券余额", unit: "亿元", note: "沪深融券余额合计" },
};

const groupTypeLabels: Record<string, string> = {
  broad: "宽基",
  industry: "行业",
  strategy: "策略",
  theme: "主题",
  cross_border: "跨境",
  bond: "债券",
  commodity: "商品",
  money: "货币",
  other: "其他",
};

const classificationRuleLabels: Record<string, string> = {
  money: "货币产品规则",
  bond: "债券产品规则",
  cross_border: "跨境产品规则",
  commodity: "商品产品规则",
  strategy: "策略名称规则",
  healthcare: "医药行业规则",
  financial_real_estate: "金融地产规则",
  consumer: "消费行业规则",
  cyclical_resources: "周期资源规则",
  new_energy_manufacturing: "新能源制造规则",
  ai_robotics: "人工智能机器人规则",
  technology: "科技行业规则",
  broad_market: "宽基指数规则",
  unmatched: "未匹配",
};

const confidenceLabels: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

const AUTO_REFRESH_MS = 5 * 60 * 1000;

function formatFlow(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function median(values: number[]) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function compactDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value.slice(5) : value;
}

function fallbackTerminal(snapshot: DashboardSnapshot): EtfTerminalData {
  const instruments = [...snapshot.etfFlows.inflow, ...snapshot.etfFlows.outflow].map((item) => ({
    ...item,
    market: "SSE" as const,
    category: "待分类",
    groupType: "unknown",
    assetClass: "其他",
    classificationSource: "missing_versioned_terminal",
    classificationConfidence: "low",
    classificationRule: "not_classified",
    includeInGroup: false,
    shares: null,
    periods: { "1d": item.value },
    estimatedCashPeriods: { "1d": null },
    coverage: 2,
  }));
  return {
    dailySeries: [],
    instruments,
    groupPeriods: [],
    availablePeriods: ["1d"],
    classification: "缺少版本化分类数据，未生成板块汇总",
    universe: { total: instruments.length, marketCounts: { SSE: instruments.length, SZSE: 0 }, categoryCounts: { "待分类": instruments.length }, periodComparable: instruments.length },
  };
}

function heatClass(value: number | null | undefined, max: number) {
  if (value === null || value === undefined) return "heat-empty";
  const strength = Math.max(1, Math.ceil((Math.abs(value) / Math.max(max, 0.01)) * 4));
  return `${value >= 0 ? "heat-in" : "heat-out"} heat-${strength}`;
}

function metricValue(snapshot: DashboardSnapshot, id: string) {
  return snapshot.metrics.find((metric) => metric.id === id);
}

const defaultMetricBasis: Record<string, { kind: EvidenceKind; label: string; detail: string }> = {
  etf: { kind: "calculated", label: "公式计算", detail: "当日官方份额 − 前一交易日官方份额" },
  southbound: { kind: "calculated", label: "公式计算", detail: "港交所官方买入成交额 − 卖出成交额" },
  margin: { kind: "official", label: "官方汇总", detail: "上交所与深交所融资余额合计" },
  northbound: { kind: "official", label: "官方汇总", detail: "港交所北向成交总额，不推断资金方向" },
};

function EvidenceBadge({ kind, label, detail }: { kind: EvidenceKind; label: string; detail: string }) {
  return <span className={`evidence-badge evidence-${kind}`} title={detail}>{label}</span>;
}

export function Dashboard({
  snapshot,
  availableDates = [],
  requestedDate = null,
  exactDateMatch = true,
  initialView = "overview",
}: DashboardProps) {
  const [view, setView] = useState<DeskView>(initialView);
  const [period, setPeriod] = useState<FlowPeriod>("1d");
  const [direction, setDirection] = useState<DirectionFilter>("all");
  const [marketFilter, setMarketFilter] = useState<MarketFilter>("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [marginDisplayMetric, setMarginDisplayMetric] = useState<MarginDisplayMetric>("balance");
  const [query, setQuery] = useState("");
  const [sorting, setSorting] = useState<SortingState>([{ id: "1d", desc: true }]);
  const [selected, setSelected] = useState<EtfTerminalInstrument | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [notice, setNotice] = useState("");
  const [draftDate, setDraftDate] = useState(snapshot.asOf);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedElement = useRef<HTMLElement | null>(null);
  const terminal = useMemo(() => snapshot.etfTerminal ?? fallbackTerminal(snapshot), [snapshot]);
  const categories = useMemo(() => Array.from(new Set(terminal.instruments.map((item) => item.category))).sort(), [terminal.instruments]);
  const availablePeriods = useMemo(() => new Set(terminal.availablePeriods ?? ["1d"]), [terminal]);
  const activeValue = (item: EtfTerminalInstrument) => item.periods[period] ?? null;

  const filteredInstruments = useMemo(() => {
    if (view !== "radar") return [];
    const normalized = query.trim().toLowerCase();
    return terminal.instruments.filter((item) => {
      const value = item.periods[period];
      const matchesDirection = direction === "all" || (direction === "inflow" ? (value ?? 0) > 0 : (value ?? 0) < 0);
      const matchesQuery = !normalized || item.name.toLowerCase().includes(normalized) || item.code.includes(normalized) || item.category.includes(normalized);
      const matchesMarket = marketFilter === "all" || item.market === marketFilter;
      const matchesCategory = categoryFilter === "all" || item.category === categoryFilter;
      return matchesDirection && matchesQuery && matchesMarket && matchesCategory;
    });
  }, [categoryFilter, direction, marketFilter, period, query, terminal.instruments, view]);

  const periodMaxima = useMemo(() => {
    const maxima = new Map<FlowPeriod, number>();
    if (view !== "radar") return maxima;
    periods.forEach(({ id }) => {
      maxima.set(id, Math.max(...terminal.instruments.map((item) => Math.abs(item.periods[id] ?? 0)), 0.01));
    });
    return maxima;
  }, [terminal.instruments, view]);

  const columns = useMemo<ColumnDef<EtfTerminalInstrument>[]>(() => [
    {
      id: "name",
      accessorFn: (row) => row.name,
      header: "官方简称",
      cell: ({ row }) => (
        <button className="fund-cell" type="button" onClick={() => setSelected(row.original)}>
          <span className="fund-rank">{String(row.index + 1).padStart(2, "0")}</span>
          <span><strong>{row.original.name}</strong><small>{row.original.code} · {row.original.market === "SZSE" ? "深市" : "沪市"} · {row.original.category}</small></span>
          <CaretRight size={15} weight="bold" />
        </button>
      ),
    },
    {
      id: "shares",
      accessorFn: (row) => row.shares ?? undefined,
      header: "规模（亿份）",
      sortUndefined: "last",
      cell: ({ row }) => row.original.shares === null ? <span className="empty-value">—</span> : row.original.shares.toFixed(1),
    },
    ...periods.map(({ id, label }): ColumnDef<EtfTerminalInstrument> => ({
      id,
      accessorFn: (row) => row.periods[id] ?? undefined,
      sortUndefined: "last",
      header: () => (
        <span className="period-heading"><strong>{label}</strong><small>{availablePeriods.has(id) ? "份额变化" : "待积累"}</small></span>
      ),
      cell: ({ row }) => {
        const value = row.original.periods[id];
        return (
          <span className={`heat-cell ${heatClass(value, periodMaxima.get(id) ?? 1)}`}>
            <strong>{formatFlow(value, 2)}</strong>
            <small>{value === null || value === undefined ? "覆盖不足" : `${row.original.periodCoverage?.[id] ?? row.original.coverage} 日覆盖`}</small>
          </span>
        );
      },
    })),
  ], [availablePeriods, periodMaxima]);

  // TanStack Table exposes mutable callbacks by design; the table stays local to this component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: filteredInstruments,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 12 } },
  });

  const groupRows = useMemo(() => terminal.groupPeriods
    .map((item) => ({
      name: item.name,
      groupType: item.groupType ?? "other",
      value: item.periods[period] ?? 0,
      componentCount: item.componentCount ?? 0,
      comparableCount: item.comparableCount?.[period] ?? 0,
    }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value)), [period, terminal.groupPeriods]);
  const totalGroupFlow = groupRows.reduce((sum, item) => sum + item.value, 0);
  const filteredTotalFlow = filteredInstruments.reduce((sum, item) => sum + (item.periods[period] ?? 0), 0);
  const positiveGroups = groupRows.filter((item) => item.value > 0);
  const crowding = positiveGroups.length
    ? Math.round((positiveGroups.slice(0, 3).reduce((sum, item) => sum + item.value, 0) / positiveGroups.reduce((sum, item) => sum + item.value, 0)) * 100)
    : 0;

  const rotationOption = useMemo(() => ({
    animationDuration: 450,
    grid: { left: 112, right: 92, top: 18, bottom: 28 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 }, formatter: (items: Array<{ dataIndex: number; name: string; value: number }>) => { const row = groupRows[items[0]?.dataIndex]; return `${items[0]?.name}<br/>${formatFlow(items[0]?.value)} 亿份<br/>${groupTypeLabels[row?.groupType] ?? row?.groupType} · ${row?.comparableCount ?? 0} 只可比`; } },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#394759" } }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    yAxis: { type: "category", data: groupRows.map((item) => item.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: "#dce4ee", fontSize: 14, fontWeight: 600 } },
    series: [{
      type: "bar",
      data: groupRows.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? "#e36464" : "#55b887", borderRadius: item.value >= 0 ? [0, 5, 5, 0] : [5, 0, 0, 5] } })),
      barMaxWidth: 18,
      label: { show: true, position: "right", color: "#bec8d5", fontFamily: "monospace", fontSize: 13, formatter: ({ value }: { value: number }) => formatFlow(value, 1) },
    }],
  }), [groupRows]);

  const trendOption = useMemo(() => ({
    animationDuration: 450,
    grid: { left: 12, right: 16, top: 22, bottom: 24, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 } },
    xAxis: { type: "category", data: terminal.dailySeries.map((item) => item.date), axisLine: { lineStyle: { color: "#394759" } }, axisTick: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    yAxis: { type: "value", axisLine: { show: false }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    series: [{ type: "bar", barMaxWidth: 22, data: terminal.dailySeries.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? "#e36464" : "#55b887", borderRadius: item.value >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) }],
  }), [terminal.dailySeries]);

  const connectSeries = useMemo(() => snapshot.connectSeries ?? [], [snapshot.connectSeries]);
  const connectChannels = useMemo(() => ({
    southbound: snapshot.connect.filter((item) => item.name.includes("南向")),
    northbound: snapshot.connect.filter((item) => item.name.includes("北向")),
  }), [snapshot.connect]);
  const marketHistoryCoverage = snapshot.marketHistoryCoverage;
  const connectTrendOption = useMemo(() => ({
    animationDuration: 450,
    legend: { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 13 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 16, right: 20, top: 44, bottom: 54, containLabel: true },
    dataZoom: [
      { type: "inside", filterMode: "none" },
      { type: "slider", height: 16, bottom: 7, borderColor: "#263648", backgroundColor: "#0b1420", fillerColor: "rgba(80,183,217,.14)", textStyle: { color: "#8795a8" } },
    ],
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 } },
    xAxis: { type: "category", data: connectSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13, formatter: compactDate } },
    yAxis: [
      { type: "value", name: "南向净买入", nameTextStyle: { color: "#8795a8", fontSize: 13 }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
      { type: "value", name: "北向成交", nameTextStyle: { color: "#8795a8", fontSize: 13 }, splitLine: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    ],
    series: [
      { name: "南向净买入（亿港元）", type: "bar", barMaxWidth: 30, data: connectSeries.map((item) => ({ value: item.southboundNet, itemStyle: { color: item.southboundNet >= 0 ? "#e36464" : "#55b887", borderRadius: item.southboundNet >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) },
      { name: "北向成交（亿元）", type: "line", yAxisIndex: 1, smooth: true, symbolSize: 7, lineStyle: { width: 2, color: "#50b7d9" }, itemStyle: { color: "#50b7d9" }, areaStyle: { color: "rgba(80,183,217,.08)" }, data: connectSeries.map((item) => item.northboundTurnover) },
    ],
  }), [connectSeries]);

  const channelOption = useMemo(() => ({
    animationDuration: 450,
    legend: { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 13 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 12, right: 14, top: 42, bottom: 54, containLabel: true },
    dataZoom: [
      { type: "inside", filterMode: "none" },
      { type: "slider", height: 16, bottom: 7, borderColor: "#263648", backgroundColor: "#0b1420", fillerColor: "rgba(80,183,217,.14)", textStyle: { color: "#8795a8" } },
    ],
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 } },
    xAxis: { type: "category", data: connectSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13, formatter: compactDate } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    series: [
      { name: "沪港通南向", type: "bar", data: connectSeries.map((item) => item.southboundSseNet), itemStyle: { color: "#d95e61" } },
      { name: "深港通南向", type: "bar", data: connectSeries.map((item) => item.southboundSzseNet), itemStyle: { color: "#f08b67" } },
    ],
  }), [connectSeries]);

  const marginPreview = useMemo(() => snapshot.marginSeries.slice(-12).map((item, index, rows) => {
    const previous = rows[index - 1]?.balance;
    const state = previous === undefined
      ? "baseline"
      : item.balance > previous
        ? "expansion"
        : item.balance < previous
          ? "contraction"
          : "flat";
    return { ...item, state, dailyChange: previous === undefined ? null : item.balance - previous };
  }), [snapshot.marginSeries]);

  const marginTrendOption = useMemo(() => {
    const definition = marginMetricDefinitions[marginDisplayMetric];
    const data = snapshot.marginSeries.map((item) => item[marginDisplayMetric] ?? null);
    const isBalance = marginDisplayMetric === "balance";
    return {
      animationDuration: 450,
      legend: isBalance ? { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 13 }, itemWidth: 14, itemHeight: 8 } : { show: false },
      grid: { left: 14, right: 18, top: 42, bottom: 54, containLabel: true },
      dataZoom: [
        { type: "inside", filterMode: "none" },
        { type: "slider", height: 16, bottom: 7, borderColor: "#263648", backgroundColor: "#0b1420", fillerColor: "rgba(80,183,217,.14)", textStyle: { color: "#8795a8" } },
      ],
      tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 }, valueFormatter: (value: number) => `${Number(value).toFixed(2)} ${definition.unit}` },
      xAxis: { type: "category", data: snapshot.marginSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13, formatter: compactDate } },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
      series: isBalance ? [
        { name: "沪深合计", type: "line", smooth: true, symbolSize: 7, data, lineStyle: { width: 3, color: "#50b7d9" }, itemStyle: { color: "#50b7d9" }, areaStyle: { color: "rgba(80,183,217,.08)" } },
        { name: "上交所", type: "line", smooth: true, data: snapshot.marginSeries.map((item) => item.sseBalance ?? null), lineStyle: { width: 1.5, color: "#e36464" }, itemStyle: { color: "#e36464" } },
        { name: "深交所", type: "line", smooth: true, data: snapshot.marginSeries.map((item) => item.szseBalance ?? null), lineStyle: { width: 1.5, color: "#f1ae42" }, itemStyle: { color: "#f1ae42" } },
      ] : [{ name: definition.label, type: "bar", barMaxWidth: 36, data, itemStyle: { color: marginDisplayMetric === "financingBuy" ? "#e36464" : "#f1ae42", borderRadius: [4, 4, 0, 0] } }],
    };
  }, [marginDisplayMetric, snapshot.marginSeries]);

  const marginInsights = useMemo(() => {
    const series = snapshot.marginSeries;
    const latest = series.at(-1);
    const previous = series.at(-2);
    const fiveDaysAgo = series.at(-6);
    const twentyDaysAgo = series.at(-21);
    const dailyChange = latest && previous ? latest.balance - previous.balance : null;
    const fiveDayChange = latest && fiveDaysAgo ? latest.balance - fiveDaysAgo.balance : null;
    const twentyDayChange = latest && twentyDaysAgo ? latest.balance - twentyDaysAgo.balance : null;
    const priorBuyValues = series
      .slice(-21, -1)
      .flatMap((item) => item.financingBuy === undefined ? [] : [item.financingBuy]);
    const buyMedian = median(priorBuyValues);
    const buyRatio = latest?.financingBuy !== undefined && buyMedian
      ? latest.financingBuy / buyMedian
      : null;
    const sseChange = latest?.sseBalance !== undefined && previous?.sseBalance !== undefined
      ? latest.sseBalance - previous.sseBalance
      : null;
    const szseChange = latest?.szseBalance !== undefined && previous?.szseBalance !== undefined
      ? latest.szseBalance - previous.szseBalance
      : null;
    const marketDriver = sseChange !== null && szseChange !== null
      ? (Math.abs(sseChange) >= Math.abs(szseChange)
        ? { label: "沪市主导", value: sseChange }
        : { label: "深市主导", value: szseChange })
      : null;
    const lendingChange = latest?.lendingBalance !== undefined && fiveDaysAgo?.lendingBalance !== undefined
      ? latest.lendingBalance - fiveDaysAgo.lendingBalance
      : null;
    return [
      {
        label: "短期杠杆趋势",
        value: formatFlow(fiveDayChange),
        unit: "亿元 / 5日",
        tone: fiveDayChange === null ? "" : fiveDayChange >= 0 ? "text-in" : "text-out",
        status: fiveDayChange === null ? "覆盖不足" : fiveDayChange >= 0 ? "余额扩张" : "余额收缩",
        note: `当日 ${formatFlow(dailyChange)} 亿元 · 20日 ${formatFlow(twentyDayChange)} 亿元`,
      },
      {
        label: "融资买入活跃度",
        value: buyRatio === null ? "—" : `${buyRatio.toFixed(2)}×`,
        unit: "20日中位数",
        tone: buyRatio === null ? "" : buyRatio >= 1.2 ? "text-amber" : "text-cyan",
        status: buyRatio === null ? "覆盖不足" : buyRatio >= 1.2 ? "偏热" : buyRatio <= 0.8 ? "偏冷" : "常态",
        note: `最新 ${formatFlow(latest?.financingBuy)} 亿元 · 基准 ${formatFlow(buyMedian)} 亿元`,
      },
      {
        label: "沪深余额增量",
        value: formatFlow(marketDriver?.value),
        unit: "亿元 / 当日",
        tone: marketDriver === null ? "" : marketDriver.value >= 0 ? "text-in" : "text-out",
        status: marketDriver?.label ?? "覆盖不足",
        note: `沪市 ${formatFlow(sseChange)} 亿元 · 深市 ${formatFlow(szseChange)} 亿元`,
      },
      {
        label: "融券余额变化",
        value: formatFlow(lendingChange),
        unit: "亿元 / 5日",
        tone: lendingChange === null ? "" : lendingChange > 0 ? "text-amber" : "text-cyan",
        status: lendingChange === null ? "覆盖不足" : lendingChange > 0 ? "融券余额抬升" : "融券余额回落",
        note: `当前融券余额 ${formatFlow(latest?.lendingBalance)} 亿元`,
      },
    ];
  }, [snapshot.marginSeries]);

  const selectedShareHistory = useMemo(() => {
    if (typeof selected?.shares !== "number") return [];
    return [
      ...shareHistoryPeriods.flatMap(({ id, label }) => {
        const change = selected.periods[id];
        return typeof change === "number"
          ? [{ label, value: selected.shares! - change }]
          : [];
      }),
      { label: "当前", value: selected.shares },
    ];
  }, [selected]);
  const selectedShareChartOption = useMemo(() => ({
    animationDuration: 350,
    grid: { left: 62, right: 18, top: 18, bottom: 42 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#111923",
      borderColor: "#334155",
      textStyle: { color: "#e7edf5", fontSize: 13 },
      valueFormatter: (value: number) => `${Number(value).toFixed(2)} 亿份`,
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: selectedShareHistory.map((item) => item.label),
      axisLine: { lineStyle: { color: "#394759" } },
      axisTick: { show: false },
      axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: "rgba(68,82,102,.32)", type: "dashed" } },
      axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 },
    },
    series: [{
      name: "官方总份额",
      type: "line",
      smooth: 0.16,
      showSymbol: true,
      symbolSize: 6,
      data: selectedShareHistory.map((item) => item.value),
      lineStyle: { width: 2.5, color: "#e36464" },
      itemStyle: { color: "#f1ae42", borderColor: "#132234", borderWidth: 2 },
      areaStyle: { color: "rgba(227,100,100,.12)" },
    }],
  }), [selectedShareHistory]);

  const latestMetric = metricValue(snapshot, "etf");
  const marginMetric = metricValue(snapshot, "margin");
  const southboundMetric = metricValue(snapshot, "southbound");
  const northboundMetric = metricValue(snapshot, "northbound");
  const evidenceFor = (id: string, basis?: { kind: EvidenceKind; label: string; detail: string }) => basis ?? defaultMetricBasis[id];
  const currentPeriod = periods.find((item) => item.id === period)?.label ?? "最近1日";
  const currentViewTitle: Record<DeskView, string> = {
    overview: "市场数据全景",
    radar: "ETF 份额雷达",
    national: "国家队持仓",
    connect: "互联互通",
    margin: "融资融券",
  };
  const displayMode = snapshot.mode.replace(/GitHub\s*Actions\s*/gi, "").trim() || "官方数据快照";
  const dateIndex = availableDates.indexOf(snapshot.asOf);
  const previousAvailableDate = dateIndex > 0 ? availableDates[dateIndex - 1] : null;
  const nextAvailableDate = dateIndex >= 0 && dateIndex < availableDates.length - 1
    ? availableDates[dateIndex + 1]
    : null;
  const openDate = (date: string) => {
    const latest = availableDates.at(-1);
    const url = new URL(window.location.href);
    if (date === latest) url.searchParams.delete("date");
    else url.searchParams.set("date", date);
    if (view === "overview") url.searchParams.delete("view");
    else url.searchParams.set("view", view);
    window.location.assign(`${url.pathname}${url.search}`);
  };

  useEffect(() => {
    setDraftDate(snapshot.asOf);
  }, [snapshot.asOf]);

  useEffect(() => {
    if (!autoRefresh || requestedDate) return;
    let lastCheck = Date.now();
    const refresh = () => {
      if (document.visibilityState === "visible") window.location.reload();
    };
    const interval = window.setInterval(refresh, AUTO_REFRESH_MS);
    const refreshAfterBackground = () => {
      if (
        document.visibilityState === "visible" &&
        Date.now() - lastCheck >= AUTO_REFRESH_MS
      ) {
        refresh();
      }
      if (document.visibilityState === "visible") lastCheck = Date.now();
    };
    document.addEventListener("visibilitychange", refreshAfterBackground);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshAfterBackground);
    };
  }, [autoRefresh, requestedDate]);

  const choosePeriod = (next: FlowPeriod) => {
    if (!availablePeriods.has(next)) {
      setNotice(`${periods.find((item) => item.id === next)?.label}尚未达到所需的官方交易日覆盖`);
      window.setTimeout(() => setNotice(""), 2400);
      return;
    }
    setPeriod(next);
    setSorting([{ id: next, desc: true }]);
  };

  useEffect(() => {
    if (!selected) return;
    lastFocusedElement.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", closeOnEscape);
      document.body.style.overflow = previousOverflow;
      window.requestAnimationFrame(() => lastFocusedElement.current?.focus());
    };
  }, [selected]);

  const clearRadarFilters = () => {
    setQuery("");
    setDirection("all");
    setMarketFilter("all");
    setCategoryFilter("all");
  };

  return (
    <main className="terminal-shell">
      <header className="terminal-topbar">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true"><Pulse size={20} weight="bold" /></span>
          <div><strong>FLOW</strong><span>啸傲的市场资金流向交易台</span></div>
        </div>
        <nav className="desk-nav" aria-label="交易台导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            return <button aria-current={view === item.id ? "page" : undefined} aria-label={item.label} className={view === item.id ? "active" : ""} key={item.id} onClick={() => { setView(item.id); const url = new URL(window.location.href); if (item.id === "national") url.searchParams.set("view", "national"); else url.searchParams.delete("view"); window.history.replaceState({}, "", url); }} title={item.label} type="button"><Icon size={17} /><span>{item.label}</span></button>;
          })}
        </nav>
        <div className="top-actions">
          <label className="global-search"><MagnifyingGlass size={18} /><input aria-label="搜索 ETF" onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && query.trim() && view !== "national") setView("radar"); }} placeholder={view === "national" ? "搜索观察池 ETF 或代码" : "搜索基金或代码，回车查看"} value={query} /></label>
          <button className="icon-button" title="打印或保存当前页面" onClick={() => window.print()} type="button"><Camera size={19} /></button>
          <button className={`refresh-toggle ${autoRefresh && !requestedDate ? "enabled" : ""}`} aria-pressed={autoRefresh && !requestedDate} disabled={Boolean(requestedDate)} onClick={() => setAutoRefresh((value) => !value)} title={requestedDate ? "历史归档不会自动切换日期" : "每 5 分钟检查最新发布数据"} type="button"><span /><em>自动更新</em></button>
          <button className="icon-button" title="刷新数据" onClick={() => window.location.reload()} type="button"><ArrowsClockwise size={19} /></button>
        </div>
      </header>

      <section className="command-strip" aria-label="数据摘要">
        <div className="command-title">
          <span className="eyebrow">官方事实与复算指标</span>
          <strong>{currentViewTitle[view]}</strong>
          <span>{snapshot.publication?.status === "confirmed" ? "完整数据截至" : "截至"} {snapshot.asOf} · {displayMode}</span>
          {requestedDate && !exactDateMatch && (
            <span className="date-fallback">{requestedDate} 非可用交易日，已显示此前最近的完整归档日</span>
          )}
        </div>
        <div className="history-date-control" aria-label="查看历史交易日">
          <button aria-label="上一个归档交易日" disabled={!previousAvailableDate} onClick={() => previousAvailableDate && openDate(previousAvailableDate)} type="button"><CaretLeft size={16} /></button>
          <label>
            <span><CalendarBlank size={15} />选择归档日期</span>
            <input
              aria-label="历史交易日期"
              max={availableDates.at(-1)}
              min={availableDates.at(0)}
              onChange={(event) => setDraftDate(event.target.value)}
              type="date"
              value={draftDate}
            />
          </label>
          <button
            className="date-apply"
            disabled={!draftDate || draftDate === snapshot.asOf}
            onClick={() => draftDate && openDate(draftDate)}
            type="button"
          >
            查看
          </button>
          <button aria-label="下一个归档交易日" disabled={!nextAvailableDate} onClick={() => nextAvailableDate && openDate(nextAvailableDate)} type="button"><CaretRight size={16} /></button>
          {snapshot.asOf !== availableDates.at(-1) && (
            <button className="date-latest" onClick={() => availableDates.at(-1) && openDate(availableDates.at(-1)!)} type="button">最新</button>
          )}
        </div>
        <div className="command-metrics">
          <span><small>ETF 份额变化<EvidenceBadge {...evidenceFor("etf", latestMetric?.basis)} /></small><strong className={latestMetric?.tone === "positive" ? "text-in" : "text-out"}>{latestMetric?.value}<em>{latestMetric?.unit}</em></strong></span>
          <span><small>南向净买入<EvidenceBadge {...evidenceFor("southbound", southboundMetric?.basis)} /></small><strong className="text-in">{southboundMetric?.value}<em>{southboundMetric?.unit}</em></strong></span>
          <span><small>融资余额<EvidenceBadge {...evidenceFor("margin", marginMetric?.basis)} /></small><strong>{marginMetric?.value}<em>{marginMetric?.unit}</em></strong></span>
          <span><small>北向成交额<EvidenceBadge {...evidenceFor("northbound", northboundMetric?.basis)} /></small><strong className="text-cyan">{northboundMetric?.value}<em>{northboundMetric?.unit}</em></strong></span>
        </div>
      </section>

      <section className="evidence-legend" aria-label="数据证据等级">
        <span><EvidenceBadge kind="official" label="官方事实" detail="交易所直接披露或两市官方值相加" />交易所直接披露或官方汇总</span>
        <span><EvidenceBadge kind="calculated" label="公式计算" detail="只使用官方字段按公开公式复算" />可从官方字段独立复算</span>
        <span><EvidenceBadge kind="official" label="日期核验" detail="深交所规模日期已与列表页发布日及前一交易日交叉校验" />深市 ETF 按官方规模日归档</span>
        <strong>未披露的数据不展示、不补造</strong>
      </section>

      {(view === "overview" || view === "radar" || view === "national") && (
        <section className="period-strip" aria-label="ETF 观察口径">
          <div className="metric-switch" aria-label="指标口径"><button className="active" type="button">份额（亿份）</button><button disabled title="等待 ETF 净值字段" type="button">金额估算</button></div>
          <div className="period-tabs" aria-label="观察周期">
            {periods.map((item) => <button aria-pressed={period === item.id} className={`${period === item.id ? "active" : ""} ${availablePeriods.has(item.id) ? "" : "unavailable"}`} key={item.id} onClick={() => choosePeriod(item.id)} type="button">{item.label}</button>)}
          </div>
          <div className="coverage-chip"><Database size={16} /><span>{availablePeriods.size} 个可用周期</span><strong>{terminal.universe?.total ?? terminal.instruments.length} 只 ETF · 沪 {terminal.universe?.marketCounts.SSE ?? terminal.instruments.filter((item) => item.market !== "SZSE").length} / 深 {terminal.universe?.marketCounts.SZSE ?? terminal.instruments.filter((item) => item.market === "SZSE").length}</strong></div>
        </section>
      )}

      {notice && <div className="toast" role="status"><Info size={17} />{notice}</div>}

      <div className="desk-content">
        {view === "overview" && (
          <>
            <section className="overview-grid">
              <article className="terminal-panel rotation-panel">
                <div className="panel-heading">
                  <div><span className="eyebrow">ETF 版本化主分类 · 公式计算</span><h2>板块份额变化</h2><p>{terminal.classificationVersion ?? "未标版本"} · {terminal.calculation ?? `当前展示 ${currentPeriod} 官方份额差`}</p></div>
                  <div className="panel-kpis"><span><small>样本净变化</small><strong className={totalGroupFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(totalGroupFlow, 1)}</strong></span><span><small>拥挤度</small><strong className="text-amber">{crowding}%</strong></span></div>
                </div>
                <ReactECharts className="rotation-chart" option={rotationOption} notMerge lazyUpdate />
                <div className="insight-row">
                  <span><i>01</i><p><strong>{groupRows[0]?.name ?? "—"}居首</strong>可见样本中份额变化最显著。</p></span>
                  <span><i>02</i><p><strong>集中度 {crowding}%</strong>前三流入方向占可见流入样本。</p></span>
                  <span><i>03</i><p><strong>口径可审计</strong>官方类型优先，低置信度产品不进入汇总。</p></span>
                </div>
              </article>

              <article className="terminal-panel trend-panel">
                <div className="panel-heading">
                  <div><span className="eyebrow">全市场 ETF</span><h2>净份额变化走势</h2><p>相邻官方交易日份额差；正值为增加</p></div>
                  <button className="link-button" onClick={() => setView("radar")} type="button">打开雷达<CaretRight size={15} /></button>
                </div>
                {terminal.dailySeries.length ? <ReactECharts className="trend-chart" option={trendOption} notMerge lazyUpdate /> : <div className="empty-chart"><Database size={30} /><strong>等待第二个交易日快照</strong><span>有连续数据后自动生成走势</span></div>}
                <div className="trend-summary">
                  <span><small>当日</small><strong className="text-in">{latestMetric?.value}</strong></span>
                  <span><small>覆盖</small><strong>{terminal.dailySeries.at(-1)?.coverage ?? 0}<em>只</em></strong></span>
                  <span><small>当前方向</small><strong className="text-cyan">{Number(latestMetric?.value.replaceAll(",", "")) >= 0 ? "份额增加" : "份额减少"}</strong></span>
                </div>
              </article>
            </section>

            <section className="signal-grid">
              <article className="terminal-panel compact-panel">
                <div className="panel-heading"><div><span className="eyebrow">互联互通</span><h2>南向有方向，北向看活跃度</h2></div><button className="link-button" onClick={() => setView("connect")} type="button">展开<CaretRight size={15} /></button></div>
                <div className="connect-cards">{snapshot.connect.map((item) => <div key={item.name}><span>{item.name}</span><strong className={`text-${item.tone === "positive" ? "in" : item.tone === "negative" ? "out" : "cyan"}`}>{item.value}<em>{item.unit}</em></strong><small>{item.description}</small></div>)}</div>
                <p className="method-note"><Info size={15} />当前官方不披露北向净流入，仅展示成交总额。</p>
              </article>
              <article className="terminal-panel compact-panel">
                <div className="panel-heading"><div><span className="eyebrow">融资融券 · 官方汇总</span><h2>杠杆余额连续变化</h2></div><button className="link-button" onClick={() => setView("margin")} type="button">展开<CaretRight size={15} /></button></div>
                <div className="margin-spark">{marginPreview.map((item, index, rows) => <div aria-label={`${item.date}，融资余额 ${item.label}${item.dailyChange === null ? "，无前日可比" : `，较前日${item.state === "expansion" ? "扩张" : item.state === "contraction" ? "收缩" : "持平"}${formatFlow(item.dailyChange)}亿元`}`} className={`margin-spark-point is-${item.state} ${index === rows.length - 1 ? "is-latest" : ""}`} key={item.date}><span className="margin-spark-bar" style={{ height: `${52 + ((item.balance - Math.min(...rows.map((point) => point.balance))) / Math.max(1, Math.max(...rows.map((point) => point.balance)) - Math.min(...rows.map((point) => point.balance)))) * 42}%` }} /><strong>{compactDate(item.date)}</strong><small>{item.label}</small></div>)}</div>
                <p className="method-note"><Info size={15} />柱高为融资余额；红色表示较前日扩张，绿色表示收缩，青色描边为最新官方数据。余额变化不直接等同多空方向。</p>
              </article>
            </section>
          </>
        )}

        {view === "radar" && (
          <section className="terminal-panel radar-panel">
            <div className="radar-heading">
              <div><span className="eyebrow">ETF 份额雷达 · 公式计算</span><h2>横向比较份额变化</h2><p>{terminal.universe?.total ?? terminal.instruments.length} 只 ETF · 红色增加，绿色减少 · 深市单日快照不生成变化值</p></div>
              <div className="radar-stats"><span><small>当前筛选净变化</small><strong className={filteredTotalFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(filteredTotalFlow, 1)}<em>亿份</em></strong></span><span><small>可比样本</small><strong className="text-cyan">{terminal.universe?.periodComparable ?? terminal.instruments.length}<em>只</em></strong></span></div>
            </div>
            <div className="radar-toolbar">
              <div className="radar-filters">
                <div className="direction-tabs">{(["all", "inflow", "outflow"] as DirectionFilter[]).map((item) => <button className={direction === item ? "active" : ""} key={item} onClick={() => setDirection(item)} type="button">{item === "all" ? "全部" : item === "inflow" ? "份额增加" : "份额减少"}</button>)}</div>
                <div className="market-tabs">{(["all", "SSE", "SZSE"] as MarketFilter[]).map((item) => <button className={marketFilter === item ? "active" : ""} key={item} onClick={() => setMarketFilter(item)} type="button">{item === "all" ? "沪深" : item === "SSE" ? "沪市" : "深市"}</button>)}</div>
                <label className="category-select"><span>主分类</span><select aria-label="ETF 主分类" onChange={(event) => setCategoryFilter(event.target.value)} value={categoryFilter}><option value="all">全部分类</option>{categories.map((category) => <option key={category} value={category}>{category} ({terminal.universe?.categoryCounts[category] ?? terminal.instruments.filter((item) => item.category === category).length})</option>)}</select></label>
              </div>
              <span className="sort-status"><ArrowsLeftRight size={16} />当前排序：{periods.find((item) => item.id === (sorting[0]?.id ?? period))?.label ?? currentPeriod} · {sorting[0]?.desc ? "降序" : "升序"}</span>
            </div>
            <div className="table-scroll-hint"><ArrowsLeftRight size={15} />左右滑动查看全部周期</div>
            <div className="radar-table-wrap">
              {table.getRowModel().rows.length ? (
                <table className="radar-table">
                  <thead>{table.getHeaderGroups().map((headerGroup) => <tr key={headerGroup.id}>{headerGroup.headers.map((header) => <th className={header.id === period ? "current-period" : ""} key={header.id}><button disabled={!header.column.getCanSort()} onClick={header.column.getToggleSortingHandler()} type="button">{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}{header.column.getIsSorted() ? <CaretDown className={header.column.getIsSorted() === "asc" ? "sort-asc" : ""} size={13} weight="bold" /> : null}</button></th>)}</tr>)}</thead>
                  <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id} onClick={() => setSelected(row.original)}>{row.getVisibleCells().map((cell) => <td className={cell.column.id === period ? "current-period" : ""} key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
                </table>
              ) : (
                <div aria-live="polite" className="radar-empty"><MagnifyingGlass size={30} /><strong>没有匹配的 ETF</strong><span>试试基金代码、简称或主分类；也可以清除当前筛选后重新查看。</span><button onClick={clearRadarFilters} type="button">清除筛选</button></div>
              )}
            </div>
            <div className="table-footer"><span>第 {table.getState().pagination.pageIndex + 1} / {Math.max(1, table.getPageCount())} 页 · {filteredInstruments.length} 条结果</span><div><button disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()} type="button"><CaretLeft size={15} />上一页</button><button disabled={!table.getCanNextPage()} onClick={() => table.nextPage()} type="button">下一页<CaretRight size={15} /></button></div></div>
          </section>
        )}

        {view === "national" && (
          <NationalTeamView
            asOf={snapshot.asOf}
            period={period}
            query={query}
            terminal={terminal}
          />
        )}

        {view === "connect" && (
          <section className="detail-grid">
            <article className="terminal-panel detail-main connect-detail">
              <div className="panel-heading connect-heading"><div className="connect-heading-copy"><span className="eyebrow">沪港通 · 深港通</span><h2>互联互通数据</h2><p>南向看公式净买入；北向只看官方成交活跃度 · {marketHistoryCoverage?.connect.points ?? connectSeries.length} 个官方历史点{marketHistoryCoverage?.connect.from ? `（${marketHistoryCoverage.connect.from} 至 ${marketHistoryCoverage.connect.to}）` : ""}</p></div><p className="connect-heading-note"><span>当日四通道</span>南向看方向 · 北向看活跃度</p></div>
              <div className="connect-top-summary" aria-label="南北互通当日摘要">
                <section className="connect-top-group connect-channel-south" aria-labelledby="southbound-channel-title">
                  <header><span className="evidence-badge evidence-calculated">公式计算</span><div><h3 id="southbound-channel-title">南向资金</h3><p>净买入方向与通道成交</p></div></header>
                  <div className="connect-top-list">{connectChannels.southbound.map((item) => <div key={item.name}><span>{item.name}</span><strong className={`text-${item.tone === "positive" ? "in" : item.tone === "negative" ? "out" : "cyan"}`}>{item.value}<em>{item.unit}</em></strong><small>{item.description}</small></div>)}</div>
                </section>
                <section className="connect-top-group connect-channel-north" aria-labelledby="northbound-channel-title">
                  <header><span className="evidence-badge evidence-official">官方披露</span><div><h3 id="northbound-channel-title">北向活跃度</h3><p>仅成交额，不代表流入方向</p></div></header>
                  <div className="connect-top-list">{connectChannels.northbound.map((item) => <div key={item.name}><span>{item.name}</span><strong className="text-cyan">{item.value}<em>{item.unit}</em></strong><small>{item.description}</small></div>)}</div>
                </section>
              </div>
              {connectSeries.length ? <div className="detail-chart-grid connect-chart-stack"><div><h3>互通走势</h3><ReactECharts className="detail-chart" option={connectTrendOption} notMerge lazyUpdate /></div><div><h3>互通拆分</h3><ReactECharts className="detail-chart" option={channelOption} notMerge lazyUpdate /></div></div> : <div className="empty-chart"><Database size={30} /><strong>等待连续交易日</strong></div>}
            </article>
            <aside className="terminal-panel methodology"><Info size={24} /><h3>披露边界</h3><p>北向当前不公开买入额、卖出额和净流入，因此系统不会用成交额替代净流入，也不会做方向推断。</p><span>北向：官方事实 · 南向：公式计算</span><div className="method-list"><p><b>南向 · 公式计算</b>买入额 − 卖出额，单位亿港元</p><p><b>北向 · 官方事实</b>仅展示总成交额与 ETF 成交额</p><p><b>未披露</b>北向方向和净流入保持缺失</p></div></aside>
          </section>
        )}

        {view === "margin" && (
          <section className="detail-grid">
            <article className="terminal-panel detail-main margin-detail">
              <div className="panel-heading"><div><span className="eyebrow">沪深两市 · 官方汇总</span><h2>两融余额与交易数据</h2><p>{marginMetricDefinitions[marginDisplayMetric].note}；{marketHistoryCoverage?.margin.points ?? snapshot.marginSeries.length} 个官方历史点{marketHistoryCoverage?.margin.from ? `（${marketHistoryCoverage.margin.from} 至 ${marketHistoryCoverage.margin.to}）` : ""}</p></div><div className="margin-hero-value"><small>最新完整数据日 · {snapshot.asOf}</small><strong className="hero-number">{marginMetric?.value}<em>{marginMetric?.unit}</em></strong></div></div>
              <div className="margin-metric-tabs">{(Object.keys(marginMetricDefinitions) as MarginDisplayMetric[]).map((item) => <button className={marginDisplayMetric === item ? "active" : ""} key={item} onClick={() => setMarginDisplayMetric(item)} type="button">{marginMetricDefinitions[item].label}</button>)}</div>
              <ReactECharts className="margin-detail-chart" option={marginTrendOption} notMerge lazyUpdate />
              <div className="margin-kpi-strip"><div><span>最新融资买入</span><strong className="text-in">{formatFlow(snapshot.marginSeries.at(-1)?.financingBuy)}<em>亿元</em></strong></div><div><span>最新融券余额</span><strong className="text-amber">{formatFlow(snapshot.marginSeries.at(-1)?.lendingBalance)}<em>亿元</em></strong></div><div><span>上交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.sseBalance)}<em>亿元</em></strong></div><div><span>深交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.szseBalance)}<em>亿元</em></strong></div></div>
              <section className="margin-insights" aria-labelledby="margin-insights-title"><div className="margin-insights-heading"><div><span className="eyebrow">从原始序列复算</span><h3 id="margin-insights-title">两融状态解读</h3></div><p>余额看存量趋势，买入额看交易活跃度；两者不能互相替代。</p></div><div className="margin-insight-grid">{marginInsights.map((item) => <article key={item.label}><header><span>{item.label}</span><small>{item.status}</small></header><strong className={item.tone}>{item.value}<em>{item.unit}</em></strong><p>{item.note}</p></article>)}</div></section>
            </article>
            <aside className="terminal-panel methodology"><Pulse size={24} /><h3>如何阅读</h3><p>{marginMetric?.delta}。余额变化只说明杠杆存量扩张或收缩，需要与指数趋势和成交额一起观察。</p><span>官方汇总 · 最新 {snapshot.asOf}</span><div className="method-list"><p><b>融资余额</b>存量杠杆规模，不叫净流入</p><p><b>融资买入</b>当日融资买入成交额</p><p><b>融券余额</b>融券卖出尚未偿还金额</p></div></aside>
          </section>
        )}
      </div>

      <footer className="terminal-footer"><span><i className="status-light" />{snapshot.publication?.status === "confirmed" ? "完整交易日已确认" : snapshot.gate === "conditional_pass" ? "条件通过" : snapshot.gate} · 观测于 {snapshot.observedAt}</span><span>份额变化不等同资金金额 · 仅供数据观察，不构成投资建议</span><a href="/api/v1/data-status">机器可读状态</a></footer>

      {selected && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelected(null)}>
          <aside aria-label={`${selected.name}详情`} aria-modal="true" className="detail-drawer" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="drawer-header"><div><span className="eyebrow">ETF 详情</span><h2>{selected.name}</h2><p>{selected.code} · {selected.market === "SZSE" ? "深交所" : "上交所"} · {selected.category} · {groupTypeLabels[selected.groupType ?? "other"] ?? selected.groupType} · {selected.assetClass ?? "未分类"}</p></div><button aria-label="关闭详情" className="icon-button" onClick={() => setSelected(null)} ref={closeButtonRef} type="button"><X size={19} /></button></div>
            <div className="drawer-hero"><span>{currentPeriod}份额变化</span><strong className={(activeValue(selected) ?? 0) >= 0 ? "text-in" : "text-out"}>{formatFlow(activeValue(selected))}<em>亿份</em></strong><small>覆盖 {selected.periodCoverage?.[period] ?? selected.coverage} 个官方交易日变化值</small></div>
            <div className="drawer-share-chart">
              <div><strong>官方总份额走势</strong><span>周期锚点复算 · 当前 {selected.shares?.toFixed(2) ?? "—"} 亿份</span></div>
              {selectedShareHistory.length ? (
                <ReactECharts className="drawer-share-echart" option={selectedShareChartOption} notMerge lazyUpdate />
              ) : (
                <div className="drawer-chart-empty"><Database size={22} />等待官方份额快照</div>
              )}
              <p>历史节点＝当前官方总份额－对应周期份额变化；不是现金净流入。</p>
            </div>
            <div className="drawer-periods">{periods.map((item) => <div className={item.id === period ? "active" : ""} key={item.id}><span>{item.label}</span><strong className={(selected.periods[item.id] ?? 0) > 0 ? "text-in" : (selected.periods[item.id] ?? 0) < 0 ? "text-out" : ""}>{formatFlow(selected.periods[item.id])}</strong><small>{selected.periods[item.id] === null || selected.periods[item.id] === undefined ? "数据积累中" : "亿份"}</small></div>)}</div>
            <div className="drawer-note"><Info size={18} /><p><strong>分类与口径</strong>{classificationRuleLabels[selected.classificationRule ?? ""] ?? "未记录规则"} · 置信度 {confidenceLabels[selected.classificationConfidence ?? ""] ?? "未知"}。当前指标是 ETF 份额变化；长周期为每日官方份额变化逐日累计，缺少历史覆盖时保持为空，不补造。估算资金仅在净值与区间份额同时完整时提供。</p></div>
          </aside>
        </div>
      )}
    </main>
  );
}
