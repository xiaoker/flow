"use client";

import {
  ArrowsClockwise,
  ArrowsLeftRight,
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
import { useMemo, useState } from "react";
import type {
  DashboardSnapshot,
  EtfTerminalData,
  EtfTerminalInstrument,
  FlowPeriod,
} from "./lib/dashboard-data";

type DashboardProps = { snapshot: DashboardSnapshot };
type DeskView = "overview" | "radar" | "connect" | "margin";
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

const navigation: Array<{ id: DeskView; label: string; icon: typeof ChartBar }> = [
  { id: "overview", label: "资金总览", icon: ChartLineUp },
  { id: "radar", label: "ETF 雷达", icon: TableIcon },
  { id: "connect", label: "互联互通", icon: ArrowsLeftRight },
  { id: "margin", label: "融资融券", icon: Pulse },
];

const marginMetricDefinitions: Record<MarginDisplayMetric, { label: string; unit: string; note: string }> = {
  balance: { label: "融资余额", unit: "亿元", note: "沪深融资余额合计" },
  financingBuy: { label: "融资买入额", unit: "亿元", note: "当日新增融资买入" },
  lendingBalance: { label: "融券余额", unit: "亿元", note: "沪深融券余额合计" },
};

function formatFlow(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function fallbackTerminal(snapshot: DashboardSnapshot): EtfTerminalData {
  const category = (name: string) => {
    if (/半导|通信|5G|科创|芯片/.test(name)) return "科技";
    if (/医药|医疗/.test(name)) return "医药";
    if (/恒生|港股/.test(name)) return "港股海外";
    if (/有色|商品/.test(name)) return "商品";
    if (/1000|300|500/.test(name)) return "宽基";
    return "其他";
  };
  const instruments = [...snapshot.etfFlows.inflow, ...snapshot.etfFlows.outflow].map((item) => ({
    ...item,
    market: "SSE" as const,
    category: category(item.name),
    assetClass: "股票",
    shares: null,
    periods: { "1d": item.value },
    coverage: 2,
  }));
  const grouped = new Map<string, number>();
  instruments.forEach((item) => grouped.set(item.category, (grouped.get(item.category) ?? 0) + item.value));
  return {
    dailySeries: [],
    instruments,
    groupPeriods: Array.from(grouped, ([name, value]) => ({ name, periods: { "1d": value } })),
    availablePeriods: ["1d"],
    classification: "基金简称关键词映射",
    universe: { total: instruments.length, marketCounts: { SSE: instruments.length, SZSE: 0 }, categoryCounts: Object.fromEntries(Array.from(grouped, ([name]) => [name, instruments.filter((item) => item.category === name).length])), periodComparable: instruments.length },
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

export function Dashboard({ snapshot }: DashboardProps) {
  const [view, setView] = useState<DeskView>("overview");
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
  const terminal = useMemo(() => snapshot.etfTerminal ?? fallbackTerminal(snapshot), [snapshot]);
  const categories = useMemo(() => Array.from(new Set(terminal.instruments.map((item) => item.category))).sort(), [terminal.instruments]);
  const availablePeriods = useMemo(() => new Set(terminal.availablePeriods ?? ["1d"]), [terminal]);
  const activeValue = (item: EtfTerminalInstrument) => item.periods[period] ?? null;

  const filteredInstruments = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return terminal.instruments.filter((item) => {
      const value = item.periods[period];
      const matchesDirection = direction === "all" || (direction === "inflow" ? (value ?? 0) > 0 : (value ?? 0) < 0);
      const matchesQuery = !normalized || item.name.toLowerCase().includes(normalized) || item.code.includes(normalized) || item.category.includes(normalized);
      const matchesMarket = marketFilter === "all" || item.market === marketFilter;
      const matchesCategory = categoryFilter === "all" || item.category === categoryFilter;
      return matchesDirection && matchesQuery && matchesMarket && matchesCategory;
    });
  }, [categoryFilter, direction, marketFilter, period, query, terminal.instruments]);

  const periodMaxima = useMemo(() => {
    const maxima = new Map<FlowPeriod, number>();
    periods.forEach(({ id }) => {
      maxima.set(id, Math.max(...terminal.instruments.map((item) => Math.abs(item.periods[id] ?? 0)), 0.01));
    });
    return maxima;
  }, [terminal.instruments]);

  const columns = useMemo<ColumnDef<EtfTerminalInstrument>[]>(() => [
    {
      id: "name",
      accessorFn: (row) => row.name,
      header: "基金名称",
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
            <small>{value === null || value === undefined ? "覆盖不足" : `${row.original.coverage} 日覆盖`}</small>
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
    .map((item) => ({ name: item.name, value: item.periods[period] ?? 0 }))
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
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 12 }, formatter: (items: Array<{ name: string; value: number }>) => `${items[0]?.name}<br/>${formatFlow(items[0]?.value)} 亿份` },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#394759" } }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    yAxis: { type: "category", data: groupRows.map((item) => item.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: "#dce4ee", fontSize: 12, fontWeight: 600 } },
    series: [{
      type: "bar",
      data: groupRows.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? "#e36464" : "#55b887", borderRadius: item.value >= 0 ? [0, 5, 5, 0] : [5, 0, 0, 5] } })),
      barMaxWidth: 18,
      label: { show: true, position: "right", color: "#bec8d5", fontFamily: "monospace", fontSize: 11, formatter: ({ value }: { value: number }) => formatFlow(value, 1) },
    }],
  }), [groupRows]);

  const trendOption = useMemo(() => ({
    animationDuration: 450,
    grid: { left: 12, right: 16, top: 22, bottom: 24, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 12 } },
    xAxis: { type: "category", data: terminal.dailySeries.map((item) => item.date), axisLine: { lineStyle: { color: "#394759" } }, axisTick: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    yAxis: { type: "value", axisLine: { show: false }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    series: [{ type: "bar", barMaxWidth: 22, data: terminal.dailySeries.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? "#e36464" : "#55b887", borderRadius: item.value >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) }],
  }), [terminal.dailySeries]);

  const connectSeries = useMemo(() => snapshot.connectSeries ?? [], [snapshot.connectSeries]);
  const connectTrendOption = useMemo(() => ({
    animationDuration: 450,
    legend: { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 11 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 16, right: 20, top: 44, bottom: 26, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 12 } },
    xAxis: { type: "category", data: connectSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    yAxis: [
      { type: "value", name: "南向净买入", nameTextStyle: { color: "#8795a8", fontSize: 11 }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
      { type: "value", name: "北向成交", nameTextStyle: { color: "#8795a8", fontSize: 11 }, splitLine: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    ],
    series: [
      { name: "南向净买入（亿港元）", type: "bar", barMaxWidth: 30, data: connectSeries.map((item) => ({ value: item.southboundNet, itemStyle: { color: item.southboundNet >= 0 ? "#e36464" : "#55b887", borderRadius: item.southboundNet >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) },
      { name: "北向成交（亿元）", type: "line", yAxisIndex: 1, smooth: true, symbolSize: 7, lineStyle: { width: 2, color: "#50b7d9" }, itemStyle: { color: "#50b7d9" }, areaStyle: { color: "rgba(80,183,217,.08)" }, data: connectSeries.map((item) => item.northboundTurnover) },
    ],
  }), [connectSeries]);

  const channelOption = useMemo(() => ({
    animationDuration: 450,
    legend: { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 11 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 12, right: 14, top: 42, bottom: 26, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 12 } },
    xAxis: { type: "category", data: connectSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
    series: [
      { name: "沪港通南向", type: "bar", data: connectSeries.map((item) => item.southboundSseNet), itemStyle: { color: "#d95e61" } },
      { name: "深港通南向", type: "bar", data: connectSeries.map((item) => item.southboundSzseNet), itemStyle: { color: "#f08b67" } },
    ],
  }), [connectSeries]);

  const marginTrendOption = useMemo(() => {
    const definition = marginMetricDefinitions[marginDisplayMetric];
    const data = snapshot.marginSeries.map((item) => item[marginDisplayMetric] ?? null);
    const isBalance = marginDisplayMetric === "balance";
    return {
      animationDuration: 450,
      legend: isBalance ? { top: 2, right: 10, textStyle: { color: "#9ba8ba", fontSize: 11 }, itemWidth: 14, itemHeight: 8 } : { show: false },
      grid: { left: 14, right: 18, top: 42, bottom: 28, containLabel: true },
      tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 12 }, valueFormatter: (value: number) => `${Number(value).toFixed(2)} ${definition.unit}` },
      xAxis: { type: "category", data: snapshot.marginSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
      yAxis: { type: "value", scale: true, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 11 } },
      series: isBalance ? [
        { name: "沪深合计", type: "line", smooth: true, symbolSize: 7, data, lineStyle: { width: 3, color: "#50b7d9" }, itemStyle: { color: "#50b7d9" }, areaStyle: { color: "rgba(80,183,217,.08)" } },
        { name: "上交所", type: "line", smooth: true, data: snapshot.marginSeries.map((item) => item.sseBalance ?? null), lineStyle: { width: 1.5, color: "#e36464" }, itemStyle: { color: "#e36464" } },
        { name: "深交所", type: "line", smooth: true, data: snapshot.marginSeries.map((item) => item.szseBalance ?? null), lineStyle: { width: 1.5, color: "#f1ae42" }, itemStyle: { color: "#f1ae42" } },
      ] : [{ name: definition.label, type: "bar", barMaxWidth: 36, data, itemStyle: { color: marginDisplayMetric === "financingBuy" ? "#e36464" : "#f1ae42", borderRadius: [4, 4, 0, 0] } }],
    };
  }, [marginDisplayMetric, snapshot.marginSeries]);

  const latestMetric = metricValue(snapshot, "etf");
  const marginMetric = metricValue(snapshot, "margin");
  const southboundMetric = metricValue(snapshot, "southbound");
  const currentPeriod = periods.find((item) => item.id === period)?.label ?? "最近1日";

  const choosePeriod = (next: FlowPeriod) => {
    if (!availablePeriods.has(next)) {
      setNotice(`${periods.find((item) => item.id === next)?.label}需要继续积累官方日快照`);
      window.setTimeout(() => setNotice(""), 2400);
      return;
    }
    setPeriod(next);
    setSorting([{ id: next, desc: true }]);
  };

  return (
    <main className="terminal-shell">
      <header className="terminal-topbar">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true"><Pulse size={20} weight="bold" /></span>
          <div><strong>FLOW</strong><span>个人资金流交易台</span></div>
        </div>
        <nav className="desk-nav" aria-label="交易台导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            return <button className={view === item.id ? "active" : ""} key={item.id} onClick={() => setView(item.id)} type="button"><Icon size={17} /><span>{item.label}</span></button>;
          })}
        </nav>
        <div className="top-actions">
          <label className="global-search"><MagnifyingGlass size={18} /><input aria-label="搜索 ETF" onChange={(event) => setQuery(event.target.value)} placeholder="搜索基金或代码" value={query} /></label>
          <button className="icon-button" title="打印或保存当前页面" onClick={() => window.print()} type="button"><Camera size={19} /></button>
          <button className={`refresh-toggle ${autoRefresh ? "enabled" : ""}`} aria-pressed={autoRefresh} onClick={() => setAutoRefresh((value) => !value)} type="button"><span /><em>自动更新</em></button>
          <button className="icon-button" title="刷新数据" onClick={() => window.location.reload()} type="button"><ArrowsClockwise size={19} /></button>
        </div>
      </header>

      <section className="command-strip" aria-label="数据摘要">
        <div className="command-title">
          <span className="eyebrow">官方资金快照</span>
          <strong>{view === "overview" ? "资金流全景" : view === "radar" ? "ETF 份额雷达" : view === "connect" ? "互联互通" : "融资融券"}</strong>
          <span>截至 {snapshot.asOf} · {snapshot.mode}</span>
        </div>
        <div className="command-metrics">
          <span><small>ETF 净变化</small><strong className={latestMetric?.tone === "positive" ? "text-in" : "text-out"}>{latestMetric?.value}<em>{latestMetric?.unit}</em></strong></span>
          <span><small>南向净买入</small><strong className="text-in">{southboundMetric?.value}<em>{southboundMetric?.unit}</em></strong></span>
          <span><small>融资余额</small><strong>{marginMetric?.value}<em>{marginMetric?.unit}</em></strong></span>
          <span><small>质量闸门</small><strong className="text-cyan">{snapshot.quality.pass}/{snapshot.quality.pass + snapshot.quality.warn}</strong></span>
        </div>
      </section>

      <section className="period-strip">
        <div className="metric-switch" aria-label="指标口径"><button className="active" type="button">份额（亿份）</button><button disabled title="等待 ETF 净值字段" type="button">金额估算</button></div>
        <div className="period-tabs" aria-label="观察周期">
          {periods.map((item) => <button aria-pressed={period === item.id} className={`${period === item.id ? "active" : ""} ${availablePeriods.has(item.id) ? "" : "unavailable"}`} key={item.id} onClick={() => choosePeriod(item.id)} type="button">{item.label}</button>)}
        </div>
        <div className="coverage-chip"><Database size={16} /><span>{availablePeriods.size} 个可用周期</span><strong>{terminal.universe?.total ?? terminal.instruments.length} 只 ETF · 沪 {terminal.universe?.marketCounts.SSE ?? terminal.instruments.filter((item) => item.market !== "SZSE").length} / 深 {terminal.universe?.marketCounts.SZSE ?? terminal.instruments.filter((item) => item.market === "SZSE").length}</strong></div>
      </section>

      {notice && <div className="toast" role="status"><Info size={17} />{notice}</div>}

      <div className="desk-content">
        {view === "overview" && (
          <>
            <section className="overview-grid">
              <article className="terminal-panel rotation-panel">
                <div className="panel-heading">
                  <div><span className="eyebrow">ETF 分类样本</span><h2>板块资金轮动</h2><p>按基金简称归类 · 当前展示 {currentPeriod} 份额变化</p></div>
                  <div className="panel-kpis"><span><small>样本净变化</small><strong className={totalGroupFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(totalGroupFlow, 1)}</strong></span><span><small>拥挤度</small><strong className="text-amber">{crowding}%</strong></span></div>
                </div>
                <ReactECharts className="rotation-chart" option={rotationOption} notMerge lazyUpdate />
                <div className="insight-row">
                  <span><i>01</i><p><strong>{groupRows[0]?.name ?? "—"}居首</strong>可见样本中资金变化最显著。</p></span>
                  <span><i>02</i><p><strong>集中度 {crowding}%</strong>前三流入方向占可见流入样本。</p></span>
                  <span><i>03</i><p><strong>口径有限</strong>分类来自简称映射，点击雷达核对 ETF。</p></span>
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
                <div className="panel-heading"><div><span className="eyebrow">融资融券</span><h2>杠杆资金连续变化</h2></div><button className="link-button" onClick={() => setView("margin")} type="button">展开<CaretRight size={15} /></button></div>
                <div className="margin-spark">{snapshot.marginSeries.map((item, index) => <div key={item.date}><span className={index === snapshot.marginSeries.length - 1 ? "latest" : ""} style={{ height: `${52 + ((item.balance - Math.min(...snapshot.marginSeries.map((point) => point.balance))) / Math.max(1, Math.max(...snapshot.marginSeries.map((point) => point.balance)) - Math.min(...snapshot.marginSeries.map((point) => point.balance)))) * 42}%` }} /><strong>{item.date}</strong><small>{item.label}</small></div>)}</div>
                <p className="method-note"><Info size={15} />余额变化代表杠杆规模扩张或收缩，不直接等同多空方向。</p>
              </article>
            </section>
          </>
        )}

        {view === "radar" && (
          <section className="terminal-panel radar-panel">
            <div className="radar-heading">
              <div><span className="eyebrow">ETF 份额雷达</span><h2>横向比较资金持续性</h2><p>{terminal.universe?.total ?? terminal.instruments.length} 只 ETF · 红色增加，绿色减少 · 深市单日快照不生成变化值</p></div>
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
              <table className="radar-table">
                <thead>{table.getHeaderGroups().map((headerGroup) => <tr key={headerGroup.id}>{headerGroup.headers.map((header) => <th className={header.id === period ? "current-period" : ""} key={header.id}><button disabled={!header.column.getCanSort()} onClick={header.column.getToggleSortingHandler()} type="button">{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}{header.column.getIsSorted() ? <CaretDown className={header.column.getIsSorted() === "asc" ? "sort-asc" : ""} size={13} weight="bold" /> : null}</button></th>)}</tr>)}</thead>
                <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id} onClick={() => setSelected(row.original)}>{row.getVisibleCells().map((cell) => <td className={cell.column.id === period ? "current-period" : ""} key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
              </table>
            </div>
            <div className="table-footer"><span>第 {table.getState().pagination.pageIndex + 1} / {Math.max(1, table.getPageCount())} 页 · {filteredInstruments.length} 条结果</span><div><button disabled={!table.getCanPreviousPage()} onClick={() => table.previousPage()} type="button"><CaretLeft size={15} />上一页</button><button disabled={!table.getCanNextPage()} onClick={() => table.nextPage()} type="button">下一页<CaretRight size={15} /></button></div></div>
          </section>
        )}

        {view === "connect" && (
          <section className="detail-grid">
            <article className="terminal-panel detail-main connect-detail">
              <div className="panel-heading"><div><span className="eyebrow">沪港通 · 深港通</span><h2>互联互通资金面</h2><p>南向看净买入方向；北向看成交活跃度</p></div><div className="panel-kpis"><span><small>南向最新</small><strong className="text-in">{formatFlow(connectSeries.at(-1)?.southboundNet)}<em>亿港元</em></strong></span><span><small>北向 ETF 成交</small><strong className="text-cyan">{connectSeries.at(-1)?.northboundEtfTurnover.toFixed(1) ?? "—"}<em>亿元</em></strong></span></div></div>
              {connectSeries.length ? <div className="detail-chart-grid"><div><h3>跨境资金走势</h3><ReactECharts className="detail-chart" option={connectTrendOption} notMerge lazyUpdate /></div><div><h3>南向通道拆分</h3><ReactECharts className="detail-chart" option={channelOption} notMerge lazyUpdate /></div></div> : <div className="empty-chart"><Database size={30} /><strong>等待连续交易日</strong></div>}
              <div className="connect-kpi-strip">{snapshot.connect.map((item) => <div key={item.name}><span>{item.name}</span><strong className={`text-${item.tone === "positive" ? "in" : item.tone === "negative" ? "out" : "cyan"}`}>{item.value}<em>{item.unit}</em></strong><small>{item.description}</small></div>)}</div>
            </article>
            <aside className="terminal-panel methodology"><Info size={24} /><h3>披露边界</h3><p>北向资金当前不公开买入额、卖出额和净流入，因此系统不会用成交额替代净流入，也不会做方向推断。</p><span>口径状态：官方可验证</span><div className="method-list"><p><b>南向</b>买入额 − 卖出额，单位亿港元</p><p><b>北向</b>展示总成交额与 ETF 成交额</p><p><b>更新</b>每个交易日生成快照</p></div></aside>
          </section>
        )}

        {view === "margin" && (
          <section className="detail-grid">
            <article className="terminal-panel detail-main margin-detail">
              <div className="panel-heading"><div><span className="eyebrow">沪深两市合计</span><h2>两融资金趋势</h2><p>{marginMetricDefinitions[marginDisplayMetric].note}；不直接解释为看多或看空</p></div><strong className="hero-number">{marginMetric?.value}<em>{marginMetric?.unit}</em></strong></div>
              <div className="margin-metric-tabs">{(Object.keys(marginMetricDefinitions) as MarginDisplayMetric[]).map((item) => <button className={marginDisplayMetric === item ? "active" : ""} key={item} onClick={() => setMarginDisplayMetric(item)} type="button">{marginMetricDefinitions[item].label}</button>)}</div>
              <ReactECharts className="margin-detail-chart" option={marginTrendOption} notMerge lazyUpdate />
              <div className="margin-kpi-strip"><div><span>最新融资买入</span><strong className="text-in">{formatFlow(snapshot.marginSeries.at(-1)?.financingBuy)}<em>亿元</em></strong></div><div><span>最新融券余额</span><strong className="text-amber">{formatFlow(snapshot.marginSeries.at(-1)?.lendingBalance)}<em>亿元</em></strong></div><div><span>上交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.sseBalance)}<em>亿元</em></strong></div><div><span>深交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.szseBalance)}<em>亿元</em></strong></div></div>
              <div className="margin-history compact-history">{snapshot.marginSeries.map((item) => <div key={item.date}><time>{item.date}</time><strong>{item.label}</strong><span className={item.change === null || item.change === undefined ? "" : item.change >= 0 ? "text-in" : "text-out"}>{item.change === null || item.change === undefined ? "基准" : `${formatFlow(item.change, 2)} 亿元`}</span></div>)}</div>
            </article>
            <aside className="terminal-panel methodology"><Pulse size={24} /><h3>当前判断</h3><p>{marginMetric?.delta}。连续回落说明杠杆规模收缩，需要与指数趋势和成交额一起观察。</p><span>最新：{snapshot.asOf}</span><div className="method-list"><p><b>融资余额</b>存量杠杆规模</p><p><b>融资买入</b>当日新增活跃度</p><p><b>融券余额</b>融券卖出尚未偿还金额</p></div></aside>
          </section>
        )}
      </div>

      <footer className="terminal-footer"><span><i className="status-light" />{snapshot.gate === "conditional_pass" ? "条件通过" : snapshot.gate} · 观测于 {snapshot.observedAt}</span><span>份额变化不等同资金金额 · 仅供数据观察，不构成投资建议</span><a href="/api/v1/data-status">机器可读状态</a></footer>

      {selected && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelected(null)}>
          <aside aria-label={`${selected.name}详情`} aria-modal="true" className="detail-drawer" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="drawer-header"><div><span className="eyebrow">ETF 详情</span><h2>{selected.name}</h2><p>{selected.code} · {selected.market === "SZSE" ? "深交所" : "上交所"} · {selected.category} · {selected.assetClass ?? "未分类"}</p></div><button aria-label="关闭详情" className="icon-button" onClick={() => setSelected(null)} type="button"><X size={19} /></button></div>
            <div className="drawer-hero"><span>{currentPeriod}份额变化</span><strong className={(activeValue(selected) ?? 0) >= 0 ? "text-in" : "text-out"}>{formatFlow(activeValue(selected))}<em>亿份</em></strong><small>覆盖 {selected.coverage} 个官方快照</small></div>
            <div className="drawer-periods">{periods.map((item) => <div key={item.id}><span>{item.label}</span><strong className={(selected.periods[item.id] ?? 0) > 0 ? "text-in" : (selected.periods[item.id] ?? 0) < 0 ? "text-out" : ""}>{formatFlow(selected.periods[item.id])}</strong><small>{selected.periods[item.id] === null || selected.periods[item.id] === undefined ? "数据积累中" : "亿份"}</small></div>)}</div>
            <div className="drawer-note"><Info size={18} /><p><strong>如何阅读</strong>当前指标是 ETF 份额变化，不包含净值，因此不等同于资金金额。{selected.market === "SZSE" ? "深市当前只有官方单日快照，变化值保持为空。" : "历史周期会随每日快照自然积累。"}</p></div>
          </aside>
        </div>
      )}
    </main>
  );
}
