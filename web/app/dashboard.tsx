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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import type {
  DashboardSnapshot,
  EvidenceKind,
  EtfStockFlowData,
  EtfTerminalData,
  EtfTerminalInstrument,
  FlowPeriod,
  StockSearchItem,
} from "./lib/dashboard-data";
import {
  getNationalAvailablePeriods,
  nationalTeamEtfs,
  NationalTeamView,
} from "./national-team-view";
import { EtfPressureView } from "./etf-pressure-view";
import { SectorRotationView } from "./sector-rotation-view";
import {
  getEffectiveEtfSharePeriods,
  getSelectableEtfSharePeriods,
} from "./lib/etf-period-availability";
import { normalizeSearchText, searchTextMatches } from "./lib/search-utils";

type DashboardProps = {
  snapshot: DashboardSnapshot;
  availableDates?: string[];
  releaseId?: string | null;
  requestedDate?: string | null;
  exactDateMatch?: boolean;
  initialView?: DeskView;
  onRefresh?: () => void | Promise<void>;
  onAutoRefresh?: () => void | Promise<void>;
  onViewChange?: (view: DeskView) => void | Promise<void>;
  onLoadRadarFlow?: () => Promise<EtfStockFlowData | null>;
  refreshing?: boolean;
  refreshError?: string | null;
  viewLoadError?: string | null;
  viewLoading?: boolean;
};
type DeskView = "overview" | "rotation" | "radar" | "national" | "connect" | "margin" | "pressure";
type DirectionFilter = "all" | "inflow" | "outflow";
type MarketFilter = "all" | "SSE" | "SZSE";
type MarginDisplayMetric = "balance" | "financingBuy" | "lendingBalance";
type EtfDisplayMetric = "shares" | "cash";
type EtfDetailHistoryPoint = { date: string; shares: number; delta: number | null };
type EtfHistoryBasis = "official_daily_change_replay" | "official_daily_share_snapshots";

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

const shareHistoryLookbacks: Partial<Record<FlowPeriod, number>> = {
  "1d": 1,
  "3d": 3,
  "1w": 5,
  "2w": 10,
  "1m": 21,
  "3m": 63,
  "6m": 126,
  "12m": 252,
};

const navigation: Array<{ id: DeskView; label: string; icon: typeof ChartBar }> = [
  { id: "overview", label: "数据总览", icon: ChartLineUp },
  { id: "rotation", label: "板块轮动", icon: ArrowsClockwise },
  { id: "radar", label: "ETF 雷达", icon: TableIcon },
  { id: "pressure", label: "ETF穿透", icon: ChartLineUp },
  { id: "national", label: "国家队持仓", icon: Buildings },
  { id: "connect", label: "南北互通", icon: ArrowsLeftRight },
  { id: "margin", label: "融资融券", icon: Pulse },
];

const marginMetricDefinitions: Record<MarginDisplayMetric, { label: string; unit: string; note: string }> = {
  balance: { label: "融资余额", unit: "亿元", note: "沪深北融资余额合计" },
  financingBuy: { label: "融资买入额", unit: "亿元", note: "当日新增融资买入" },
  lendingBalance: { label: "融券余额", unit: "亿元", note: "沪深北融券余额合计" },
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
  unclassified: "未归类",
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

const AUTO_REFRESH_MS = 30 * 60 * 1000;

function formatFlow(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

function formatShares(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  if (absolute >= 100_000_000) return `${sign}${(absolute / 100_000_000).toFixed(2)}亿股`;
  if (absolute >= 10_000) return `${sign}${(absolute / 10_000).toFixed(2)}万股`;
  return `${sign}${absolute.toFixed(0)}股`;
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
    scope: "SSE+SZSE",
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
  margin: { kind: "official", label: "官方汇总", detail: "上交所、深交所与北交所融资余额合计" },
  northbound: { kind: "official", label: "官方汇总", detail: "港交所北向成交总额，不推断资金方向" },
};

function EvidenceBadge({ kind, label, detail }: { kind: EvidenceKind; label: string; detail: string }) {
  return <span className={`evidence-badge evidence-${kind}`} title={detail}>{label}</span>;
}

export function Dashboard({
  snapshot,
  availableDates = [],
  releaseId = null,
  requestedDate = null,
  exactDateMatch = true,
  initialView = "overview",
  onRefresh,
  onAutoRefresh,
  onViewChange,
  onLoadRadarFlow,
  refreshing = false,
  refreshError = null,
  viewLoadError = null,
  viewLoading = false,
}: DashboardProps) {
  const [view, setView] = useState<DeskView>(initialView);
  const [period, setPeriod] = useState<FlowPeriod>("1d");
  const [direction, setDirection] = useState<DirectionFilter>("all");
  const [marketFilter, setMarketFilter] = useState<MarketFilter>("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [etfDisplayMetric, setEtfDisplayMetric] = useState<EtfDisplayMetric>("shares");
  const [marginDisplayMetric, setMarginDisplayMetric] = useState<MarginDisplayMetric>("balance");
  const [query, setQuery] = useState("");
  const [globalQuery, setGlobalQuery] = useState("");
  const [stockSearchItems, setStockSearchItems] = useState<StockSearchItem[]>([]);
  const [stockSearchLoading, setStockSearchLoading] = useState(false);
  const [stockSearchError, setStockSearchError] = useState<string | null>(null);
  const [stockSearchLoadedFor, setStockSearchLoadedFor] = useState<string | null>(null);
  const [searchActiveIndex, setSearchActiveIndex] = useState(0);
  const [sorting, setSorting] = useState<SortingState>([{ id: "1d", desc: true }]);
  const [selected, setSelected] = useState<EtfTerminalInstrument | null>(null);
  const [selectedHistory, setSelectedHistory] = useState<EtfDetailHistoryPoint[]>([]);
  const [selectedHistoryBasis, setSelectedHistoryBasis] = useState<EtfHistoryBasis | null>(null);
  const [selectedHistoryLoading, setSelectedHistoryLoading] = useState(false);
  const [radarFlowLoading, setRadarFlowLoading] = useState(false);
  const [radarFlowError, setRadarFlowError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [notice, setNotice] = useState("");
  const [draftDate, setDraftDate] = useState(snapshot.asOf);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedElement = useRef<HTMLElement | null>(null);
  const radarFlowAttemptedFor = useRef<string | null>(null);
  const terminal = useMemo(() => snapshot.etfTerminal ?? fallbackTerminal(snapshot), [snapshot]);
  const searchableEtfs = useMemo(() => snapshot.etfSearch?.length
    ? snapshot.etfSearch.map((item) => ({
        ...item,
        market: item.market === "SZSE" ? "SZSE" as const : "SSE" as const,
        category: item.category ?? "未归类",
        shares: null,
        periods: {},
        coverage: 0,
      }))
    : terminal.instruments,
    [snapshot.etfSearch, terminal.instruments]);
  const categories = useMemo(() => Array.from(new Set(terminal.instruments.map((item) => item.category))).sort(), [terminal.instruments]);
  const shareAvailablePeriods = useMemo(
    () => new Set(getEffectiveEtfSharePeriods(terminal)),
    [terminal],
  );
  const shareSelectablePeriods = useMemo(
    () => new Set(getSelectableEtfSharePeriods(terminal)),
    [terminal],
  );
  const cashAvailable = Boolean(
    terminal.cashFlow?.available ||
      (terminal.cashAvailablePeriods?.length ?? 0) > 0 ||
      terminal.instruments.some((item) => typeof item.estimatedCashPeriods?.["1d"] === "number"),
  );
  const metricLabel = etfDisplayMetric === "cash" ? "金额估算" : "份额变化";
  const metricUnit = etfDisplayMetric === "cash" ? "亿元" : "亿份";
  const metricAvailablePeriods = useMemo(() => {
    if (etfDisplayMetric === "shares") return shareAvailablePeriods;
    if (terminal.cashAvailablePeriods?.length) return new Set(terminal.cashAvailablePeriods);
    return new Set(
      periods
        .map((item) => item.id)
        .filter((id) => terminal.instruments.some((item) => typeof item.estimatedCashPeriods?.[id] === "number")),
    );
  }, [etfDisplayMetric, shareAvailablePeriods, terminal.cashAvailablePeriods, terminal.instruments]);
  const nationalAvailablePeriods = useMemo(
    () => getNationalAvailablePeriods(terminal),
    [terminal],
  );
  // Selection is based on the release's declared windows.  Coverage remains
  // separate so an empty/partial window can be inspected without inventing a
  // value or making the period control appear broken.
  const activeSelectablePeriods = view === "national"
    ? nationalAvailablePeriods
    : shareSelectablePeriods;
  // Keep the existing name for the selection gate: it now represents
  // declared query windows, while metricAvailablePeriods remains evidence.
  const activeAvailablePeriods = activeSelectablePeriods;
  const metricValueFor = useCallback((item: EtfTerminalInstrument, id: FlowPeriod) =>
    etfDisplayMetric === "cash" ? item.estimatedCashPeriods?.[id] : item.periods[id], [etfDisplayMetric]);
  const metricCoverageFor = useCallback((item: EtfTerminalInstrument, id: FlowPeriod) =>
    etfDisplayMetric === "cash"
      ? item.cashPeriodCoverage?.[id] ?? 0
      : item.periodCoverage?.[id] ?? item.coverage, [etfDisplayMetric]);
  const selectedCode = selected?.code;
  const selectedMarket = selected?.market ?? "SSE";

  useEffect(() => {
    const normalized = normalizeSearchText(globalQuery);
    const searchReleaseKey = `${releaseId ?? "date"}:${snapshot.asOf}`;
    if (!normalized || stockSearchLoadedFor === searchReleaseKey) return;
    const controller = new AbortController();
    setStockSearchLoading(true);
    setStockSearchError(null);
    setStockSearchItems([]);
    fetch(`/api/v2/stock-search?date=${encodeURIComponent(snapshot.asOf)}`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 404 ? "stock_model_not_published_for_date" : `stock_search_${response.status}`);
        const responseReleaseId = response.headers.get("x-flow-release-id");
        const responseAsOf = response.headers.get("x-flow-as-of");
        if ((releaseId && responseReleaseId !== releaseId) || (responseAsOf && responseAsOf !== snapshot.asOf)) {
          throw new Error("release_pending");
        }
        const payload = await response.json() as { items?: StockSearchItem[] };
        const publishedItems = Array.isArray(payload.items) ? payload.items : [];
        setStockSearchItems(publishedItems);
        setStockSearchLoadedFor(searchReleaseKey);
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setStockSearchItems([]);
        setStockSearchError(cause instanceof Error ? cause.message : "stock_search_unavailable");
        setStockSearchLoadedFor(searchReleaseKey);
      })
      .finally(() => {
        if (!controller.signal.aborted) setStockSearchLoading(false);
      });
    return () => controller.abort();
  }, [globalQuery, releaseId, snapshot.asOf, stockSearchLoadedFor]);

  useEffect(() => {
    if (!selected) return;
    const refreshed = terminal.instruments.find(
      (item) => item.code === selected.code && item.market === selected.market,
    );
    if (refreshed && refreshed !== selected) setSelected(refreshed);
  }, [selected, terminal.instruments]);

  useEffect(() => {
    const attemptKey = selected ? `${snapshot.asOf}:${selected.market ?? "SSE"}:${selected.code}` : null;
    const selectedIsHydrated = Boolean(selected && terminal.instruments.some(
      (item) => item.code === selected.code && item.market === selected.market,
    ));
    if (!selected) radarFlowAttemptedFor.current = null;
    if (!selected || !selectedIsHydrated || snapshot.etfStockFlow || !onLoadRadarFlow || view !== "radar" || radarFlowAttemptedFor.current === attemptKey) {
      setRadarFlowLoading(false);
      return;
    }
    let active = true;
    radarFlowAttemptedFor.current = attemptKey;
    setRadarFlowLoading(true);
    setRadarFlowError(null);
    void onLoadRadarFlow()
      .catch((cause: unknown) => {
        if (active) setRadarFlowError(cause instanceof Error ? cause.message : "ETF 穿透数据加载失败");
      })
      .finally(() => {
        if (active) setRadarFlowLoading(false);
      });
    return () => {
      active = false;
      if (radarFlowAttemptedFor.current === attemptKey && !snapshot.etfStockFlow) {
        radarFlowAttemptedFor.current = null;
      }
    };
  }, [onLoadRadarFlow, selected, snapshot.asOf, snapshot.etfStockFlow, terminal.instruments, view]);

  const globalSearchResults = useMemo(() => {
    const normalized = normalizeSearchText(globalQuery);
    if (!normalized) return { stocks: [] as StockSearchItem[], etfs: [] as EtfTerminalInstrument[] };
    const rank = (item: { code: string; name: string }) =>
      normalizeSearchText(item.code) === normalized ? 0 : normalizeSearchText(item.name) === normalized ? 1 : 2;
    const byMatch = <T extends { code: string; name: string }>(items: T[]) => [...items]
      .sort((left, right) => rank(left) - rank(right) || left.name.localeCompare(right.name, "zh-CN"))
      .slice(0, 6);
    return {
      stocks: byMatch(stockSearchItems.filter((item) => searchTextMatches(globalQuery, item.code, item.name))),
      etfs: byMatch(searchableEtfs.filter((item) => searchTextMatches(globalQuery, item.code, item.name))),
    };
  }, [globalQuery, searchableEtfs, stockSearchItems]);

  useEffect(() => {
    if (etfDisplayMetric === "cash" && !cashAvailable) {
      setEtfDisplayMetric("shares");
      return;
    }
    if (!activeAvailablePeriods.has(period)) {
      const first = periods.find((item) => activeAvailablePeriods.has(item.id))?.id;
      if (first) setPeriod(first);
    }
  }, [activeAvailablePeriods, cashAvailable, etfDisplayMetric, period]);

  useEffect(() => {
    if (!selectedCode) {
      setSelectedHistory([]);
      setSelectedHistoryBasis(null);
      setSelectedHistoryLoading(false);
      return;
    }
    const controller = new AbortController();
    setSelectedHistory([]);
    setSelectedHistoryBasis(null);
    setSelectedHistoryLoading(true);
    const params = new URLSearchParams({ code: selectedCode, market: selectedMarket });
    fetch(`/api/v1/etf/history?${params.toString()}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`history_${response.status}`);
        const payload = await response.json() as { history?: EtfDetailHistoryPoint[]; basis?: EtfHistoryBasis };
        setSelectedHistory(Array.isArray(payload.history) ? payload.history : []);
        setSelectedHistoryBasis(payload.basis ?? null);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setSelectedHistory([]);
        setSelectedHistoryBasis(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setSelectedHistoryLoading(false);
      });
    return () => controller.abort();
  }, [selectedCode, selectedMarket]);

  const filteredInstruments = useMemo(() => {
    if (view !== "radar") return [];
    const normalized = query.trim().toLowerCase();
    return terminal.instruments.filter((item) => {
      const value = metricValueFor(item, period);
      const matchesDirection = direction === "all" || (direction === "inflow" ? (value ?? 0) > 0 : (value ?? 0) < 0);
      const matchesQuery = !normalized || item.name.toLowerCase().includes(normalized) || item.code.includes(normalized) || item.category.includes(normalized);
      const matchesMarket = marketFilter === "all" || item.market === marketFilter;
      const matchesCategory = categoryFilter === "all" || item.category === categoryFilter;
      return matchesDirection && matchesQuery && matchesMarket && matchesCategory;
    });
  }, [categoryFilter, direction, metricValueFor, marketFilter, period, query, terminal.instruments, view]);

  const periodMaxima = useMemo(() => {
    const maxima = new Map<FlowPeriod, number>();
    if (view !== "radar") return maxima;
    periods.forEach(({ id }) => {
      maxima.set(id, Math.max(...terminal.instruments.map((item) => Math.abs(metricValueFor(item, id) ?? 0)), 0.01));
    });
    return maxima;
  }, [metricValueFor, terminal.instruments, view]);

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
      accessorFn: (row) => metricValueFor(row, id) ?? undefined,
      sortUndefined: "last",
      sortingFn: (left, right, columnId) => {
        const leftValue = left.getValue<number | undefined>(columnId);
        const rightValue = right.getValue<number | undefined>(columnId);
        if (leftValue === undefined && rightValue === undefined) return 0;
        if (leftValue === undefined) return 1;
        if (rightValue === undefined) return -1;
        return Math.abs(leftValue) - Math.abs(rightValue);
      },
      header: () => (
        <span className="period-heading"><strong>{label}</strong><small>{metricAvailablePeriods.has(id) ? metricLabel : "待积累"}</small></span>
      ),
      cell: ({ row }) => {
        const value = metricValueFor(row.original, id);
        return (
          <span className={`heat-cell ${heatClass(value, periodMaxima.get(id) ?? 1)}`}>
            <strong>{formatFlow(value, 2)}</strong>
            <small>{value === null || value === undefined ? "覆盖不足" : `${metricCoverageFor(row.original, id)} 日覆盖`}</small>
          </span>
        );
      },
    })),
  ], [metricAvailablePeriods, metricCoverageFor, metricLabel, metricValueFor, periodMaxima]);

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
      value: (() => {
        const value = etfDisplayMetric === "cash" ? item.estimatedCashPeriods?.[period] : item.periods[period];
        return typeof value === "number" ? value : null;
      })(),
      componentCount: item.componentCount ?? 0,
      comparableCount: etfDisplayMetric === "cash"
        ? item.estimatedCashCoverage?.[period] ?? 0
        : item.comparableCount?.[period] ?? 0,
    }))
    .sort((a, b) => Math.abs(b.value ?? 0) - Math.abs(a.value ?? 0)), [etfDisplayMetric, period, terminal.groupPeriods]);
  const totalGroupFlow = groupRows.some((item) => typeof item.value === "number")
    ? groupRows.reduce((sum, item) => sum + (item.value ?? 0), 0)
    : null;
  const filteredNumericValues = filteredInstruments
    .map((item) => metricValueFor(item, period))
    .filter((value): value is number => typeof value === "number");
  const filteredTotalFlow = filteredNumericValues.length
    ? filteredNumericValues.reduce((sum, value) => sum + value, 0)
    : null;
  const reconciliation = useMemo(() => {
    const precomputed = terminal.reconciliation?.[period]?.[etfDisplayMetric];
    if (precomputed) {
      return {
        ...precomputed,
        classifiedTotal: precomputed.classified,
        unclassifiedTotal: precomputed.unclassified,
        unclassifiedComparable: precomputed.unclassified,
      };
    }
    const instrumentTotal = terminal.instruments.reduce((sum, item) => {
      const value = metricValueFor(item, period);
      return typeof value === "number" ? sum + value : sum;
    }, 0);
    const groupTotal = terminal.groupPeriods.reduce((sum, item) => {
      const value = etfDisplayMetric === "cash"
        ? item.estimatedCashPeriods?.[period]
        : item.periods[period];
      return typeof value === "number" ? sum + value : sum;
    }, 0);
    const comparable = terminal.instruments.filter((item) => typeof metricValueFor(item, period) === "number");
    const increased = comparable.filter((item) => (metricValueFor(item, period) ?? 0) > 0).length;
    const decreased = comparable.filter((item) => (metricValueFor(item, period) ?? 0) < 0).length;
    const unchanged = comparable.length - increased - decreased;
    const unclassifiedTotal = terminal.instruments.filter((item) => item.category === "未归类").length;
    const classifiedTotal = terminal.instruments.length - unclassifiedTotal;
    return {
      instrumentTotal,
      groupTotal,
      delta: groupTotal - instrumentTotal,
      comparable: comparable.length,
      increased,
      decreased,
      unchanged,
      classifiedTotal,
      unclassifiedTotal,
      unclassifiedComparable: comparable.filter((item) => item.category === "未归类").length,
    };
  }, [etfDisplayMetric, metricValueFor, period, terminal.groupPeriods, terminal.instruments, terminal.reconciliation]);
  const positiveGroups = groupRows.filter((item) => typeof item.value === "number" && item.value > 0);
  const topGroup = groupRows.find((item) => typeof item.value === "number");
  const crowding = positiveGroups.length
    ? Math.round((positiveGroups.slice(0, 3).reduce((sum, item) => sum + (item.value ?? 0), 0) / positiveGroups.reduce((sum, item) => sum + (item.value ?? 0), 0)) * 100)
    : 0;

  const rotationOption = useMemo(() => ({
    animationDuration: 450,
    grid: { left: 112, right: 92, top: 18, bottom: 28 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 }, formatter: (items: Array<{ dataIndex: number; name: string; value: number | null }>) => { const row = groupRows[items[0]?.dataIndex]; const coverageLabel = etfDisplayMetric === "cash" ? "日净值覆盖" : "只可比"; const value = items[0]?.value; return `${items[0]?.name}<br/>${value === null || value === undefined ? "覆盖不足" : `${formatFlow(value)} ${metricUnit}`}<br/>${groupTypeLabels[row?.groupType] ?? row?.groupType} · ${row?.comparableCount ?? 0} ${coverageLabel}`; } },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#394759" } }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    yAxis: { type: "category", data: groupRows.map((item) => item.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: "#dce4ee", fontSize: 14, fontWeight: 600 } },
    series: [{
      type: "bar",
      data: groupRows.map((item) => ({ value: item.value, itemStyle: { color: item.value === null ? "#526176" : item.value >= 0 ? "#e36464" : "#55b887", borderRadius: item.value !== null && item.value >= 0 ? [0, 5, 5, 0] : [5, 0, 0, 5] } })),
      barMaxWidth: 18,
      label: { show: true, position: "right", color: "#bec8d5", fontFamily: "monospace", fontSize: 13, formatter: ({ value }: { value: number | null }) => value === null || value === undefined ? "覆盖不足" : formatFlow(value, 1) },
    }],
  }), [etfDisplayMetric, groupRows, metricUnit]);

  const trendOption = useMemo(() => ({
    animationDuration: 450,
    grid: { left: 12, right: 16, top: 22, bottom: 24, containLabel: true },
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 } },
    xAxis: { type: "category", data: terminal.dailySeries.map((item) => item.date), axisLine: { lineStyle: { color: "#394759" } }, axisTick: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    yAxis: { type: "value", axisLine: { show: false }, splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    series: [{ type: "bar", barMaxWidth: 22, data: terminal.dailySeries.map((item) => {
      const value = etfDisplayMetric === "cash" ? item.cashValue ?? null : item.value;
      return { value, itemStyle: { color: value === null ? "#526176" : value >= 0 ? "#e36464" : "#55b887", borderRadius: value !== null && value >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } };
    }) }],
  }), [etfDisplayMetric, terminal.dailySeries]);

  const connectSeries = useMemo(() => snapshot.connectSeries ?? [], [snapshot.connectSeries]);
  const connectChannels = useMemo(() => ({
    southbound: snapshot.connect.filter((item) => item.name.includes("南向")),
    northbound: snapshot.connect.filter((item) => item.name.includes("北向")),
  }), [snapshot.connect]);
  const marketHistoryCoverage = snapshot.marketHistoryCoverage;
  const connectTrendOption = useMemo(() => ({
    animationDuration: 450,
    legend: { type: "scroll", top: 2, left: 68, right: 10, itemGap: 16, textStyle: { color: "#9ba8ba", fontSize: 13 }, itemWidth: 14, itemHeight: 8 },
    grid: { left: 16, right: 20, top: 44, bottom: 54, containLabel: true },
    dataZoom: [
      { type: "inside", filterMode: "none" },
      { type: "slider", height: 16, bottom: 7, borderColor: "#263648", backgroundColor: "#0b1420", fillerColor: "rgba(80,183,217,.14)", textStyle: { color: "#8795a8" } },
    ],
    tooltip: { trigger: "axis", backgroundColor: "#111923", borderColor: "#334155", textStyle: { color: "#e7edf5", fontSize: 14 } },
    xAxis: { type: "category", data: connectSeries.map((item) => item.date), axisTick: { show: false }, axisLine: { lineStyle: { color: "#394759" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13, formatter: compactDate } },
    yAxis: [
      { type: "value", splitLine: { lineStyle: { color: "rgba(68,82,102,.34)", type: "dashed" } }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
      { type: "value", splitLine: { show: false }, axisLabel: { color: "#8795a8", fontFamily: "monospace", fontSize: 13 } },
    ],
    series: [
      { name: "南向净买入（亿港元）", type: "bar", barMaxWidth: 30, data: connectSeries.map((item) => ({ value: item.southboundNet, itemStyle: { color: item.southboundNet >= 0 ? "#e36464" : "#55b887", borderRadius: item.southboundNet >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) },
      { name: "北向成交（亿元）", type: "line", yAxisIndex: 1, smooth: true, symbolSize: 7, lineStyle: { width: 2, color: "#50b7d9" }, itemStyle: { color: "#50b7d9" }, areaStyle: { color: "rgba(80,183,217,.08)" }, data: connectSeries.map((item) => item.northboundTurnover) },
    ],
  }), [connectSeries]);

  const channelOption = useMemo(() => ({
    animationDuration: 450,
    legend: { type: "scroll", top: 2, left: 68, right: 10, itemGap: 16, textStyle: { color: "#9ba8ba", fontSize: 13 }, itemWidth: 14, itemHeight: 8 },
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

  const marginHasBse = useMemo(
    () => snapshot.marginSeries.some((item) => item.marginScopeVersion === "sse_szse_bse_v2"),
    [snapshot.marginSeries],
  );

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
        ...(marginHasBse ? [{ name: "北交所", type: "line", smooth: true, data: snapshot.marginSeries.map((item) => item.bseBalance ?? null), lineStyle: { width: 1.5, color: "#8c7ae6" }, itemStyle: { color: "#8c7ae6" } }] : []),
      ] : [{ name: definition.label, type: "bar", barMaxWidth: 36, data, itemStyle: { color: marginDisplayMetric === "financingBuy" ? "#e36464" : "#f1ae42", borderRadius: [4, 4, 0, 0] } }],
    };
  }, [marginDisplayMetric, marginHasBse, snapshot.marginSeries]);

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
    if (selectedHistory.length >= 2) {
      return selectedHistory.slice(-60).map((item) => ({
        label: item.date.slice(5),
        value: item.shares,
      }));
    }
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
  }, [selected, selectedHistory]);
  const selectedPeriodRows = useMemo(() => periods.map((item) => {
    if (etfDisplayMetric === "cash") {
      const rawCash = selected?.estimatedCashPeriods?.[item.id] ?? null;
      return { ...item, value: typeof rawCash === "number" ? Number(rawCash.toFixed(2)) : null };
    }
    const lookback = shareHistoryLookbacks[item.id];
    const rawValue = selectedHistory.length > (lookback ?? Number.MAX_SAFE_INTEGER) && lookback
      ? selectedHistory.at(-1)!.shares - selectedHistory.at(-(lookback + 1))!.shares
      : selected?.periods[item.id] ?? null;
    return { ...item, value: typeof rawValue === "number" ? Number(rawValue.toFixed(2)) : null };
  }), [etfDisplayMetric, selected, selectedHistory]);
  const selectedActiveValue = selectedPeriodRows.find((item) => item.id === period)?.value ?? null;
  const selectedAvailablePeriods = selectedPeriodRows.filter((item) => typeof item.value === "number");
  const selectedPendingPeriods = selectedPeriodRows.filter((item) => item.value === null);
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
      name: "官方总份额（亿份）",
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
  const selectedStockFlow = useMemo(() => {
    const flow = snapshot.etfStockFlow;
    if (!flow) return null;
    const etf = flow.etfs.find((item) => item.code === selected?.code && item.market === selected?.market);
    const directRows = etf?.stocks?.map((stock) => ({
      stockCode: stock.stockCode,
      stockName: stock.stockName,
      market: stock.market,
      listingMarket: stock.listingMarket,
      listingMarketName: stock.listingMarketName,
      value: stock.stockFlowShares,
      etfName: etf.name,
      estimated: stock.estimated,
    })) ?? [];
    // Backward-compatible fallback for an older active release that has the
    // global stock graph but predates ETF-first component lists.
    const rows = directRows.length
      ? directRows
      : flow.stocks.flatMap((stock) =>
          stock.contributors
            .filter((contributor) => contributor.etfCode === selected?.code)
            .map((contributor) => ({
              stockCode: stock.stockCode,
              stockName: stock.stockName,
              market: stock.market,
              listingMarket: stock.listingMarket,
              listingMarketName: stock.listingMarketName,
              value: contributor.stockFlowShares,
              etfName: contributor.etfName,
              estimated: contributor.estimated,
            })),
        );
    return { flow, etf, rows, covered: Boolean(etf) };
  }, [selected, snapshot.etfStockFlow]);

  const latestMetric = metricValue(snapshot, "etf");
  const marginMetric = metricValue(snapshot, "margin");
  const southboundMetric = metricValue(snapshot, "southbound");
  const northboundMetric = metricValue(snapshot, "northbound");
  const evidenceFor = (id: string, basis?: { kind: EvidenceKind; label: string; detail: string }) => basis ?? defaultMetricBasis[id];
  const currentPeriod = periods.find((item) => item.id === period)?.label ?? "最近1日";
  const latestShareValue = terminal.dailySeries.at(-1)?.value ?? Number(latestMetric?.value?.replaceAll(",", "") ?? "NaN");
  const latestCashValues = terminal.instruments
    .map((item) => item.estimatedCashPeriods?.["1d"])
    .filter((value): value is number => typeof value === "number");
  const latestCashValue = terminal.dailySeries.at(-1)?.cashValue ?? (
    latestCashValues.length
      ? latestCashValues.reduce((sum, value) => sum + value, 0)
      : terminal.reconciliation?.["1d"]?.cash?.instrumentTotal ?? null
  );
  const latestCashCoverage = terminal.dailySeries.at(-1)?.cashCoverage
    ?? (latestCashValues.length || terminal.reconciliation?.["1d"]?.cash?.comparable || 0);
  const latestDisplayValue = etfDisplayMetric === "cash"
    ? latestCashValue
    : Number.isFinite(latestShareValue) ? latestShareValue : null;
  const latestDisplayText = formatFlow(latestDisplayValue);
  const latestDisplayDirection = latestDisplayValue === null
    ? "覆盖不足"
    : latestDisplayValue >= 0
      ? `${metricLabel}增加`
      : `${metricLabel}减少`;
  const metricComparableCount = terminal.instruments.filter(
    (item) => typeof metricValueFor(item, period) === "number",
  ).length;
  const selectedPeriodCovered = metricAvailablePeriods.has(period);
  const currentViewTitle: Record<DeskView, string> = {
    overview: "市场数据全景",
    rotation: "板块资金轮动",
    radar: "ETF 份额雷达",
    pressure: "ETF穿透",
    national: "国家队持仓",
    connect: "互联互通",
    margin: "融资融券",
  };
  const displayMode = snapshot.mode.replace(/GitHub\s*Actions\s*/gi, "").trim() || "官方数据快照";
  const warningDomain = snapshot.dataStatus.find((item) => item.status === "warning");
  const dataStatus = refreshError
    ? { tone: "degraded", title: "发布检查暂不可用", detail: `${refreshError} · 当前仍展示 ${releaseId ? `release ${releaseId}` : "当前完整发布版本"}` }
    : viewLoadError
      ? { tone: "degraded", title: "当前视图暂未载入", detail: "已保留当前完整发布版本，未拼接其他日期数据" }
      : snapshot.publication?.status === "pending"
        ? { tone: "pending", title: "等待完整发布", detail: snapshot.publication.detail || "部分官方源尚未完成，当前 active release 不变" }
        : warningDomain
          ? { tone: "degraded", title: "部分数据待核验", detail: warningDomain.detail }
          : { tone: "ready", title: "完整发布已确认", detail: "当前页面各模块使用同一 active release" };
  const navigateView = (nextView: DeskView) => {
    setView(nextView);
    const url = new URL(window.location.href);
    if (nextView === "overview") url.searchParams.delete("view");
    else url.searchParams.set("view", nextView);
    window.history.replaceState({}, "", `${url.pathname}${url.search}`);
    const result = onViewChange?.(nextView);
    if (result instanceof Promise) void result.catch(() => undefined);
  };
  const openStockCode = (code: string, listingMarket?: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", "stock");
    const venue = listingMarket === "101"
      ? "SSE"
      : listingMarket === "102"
        ? "SZSE"
        : listingMarket === "103"
          ? "HKEX"
          : listingMarket === "104"
            ? "BSE"
            : "";
    if (venue) url.searchParams.set("venue", venue);
    else url.searchParams.delete("venue");
    url.searchParams.set("code", code);
    // Keep the date in every stock URL. This makes ETF→stock, search→stock
    // and a refreshed deep link resolve the same immutable release.
    url.searchParams.set("date", snapshot.asOf);
    setGlobalQuery("");
    window.location.assign(`${url.pathname}${url.search}`);
  };
  const openStockDetail = (item: StockSearchItem) => openStockCode(item.code, item.listingMarket);
  const openStockFromFlow = (row: { stockCode: string; listingMarket?: string }) => openStockCode(row.stockCode, row.listingMarket);
  const openEtfFromSearch = (item: EtfTerminalInstrument) => {
    setGlobalQuery("");
    setQuery(item.name);
    const full = terminal.instruments.find((candidate) =>
      candidate.code === item.code && candidate.market === item.market,
    );
    setSelected(full ?? item);
    if (view !== "radar") navigateView("radar");
  };
  const handleGlobalSearchKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    const options: Array<{ kind: "stock" | "etf"; item: StockSearchItem | EtfTerminalInstrument }> = [
      ...globalSearchResults.stocks.map((item) => ({ kind: "stock" as const, item })),
      ...globalSearchResults.etfs.map((item) => ({ kind: "etf" as const, item })),
    ];
    if (event.key === "Escape") {
      setGlobalQuery("");
      setSearchActiveIndex(0);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!options.length) return;
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      setSearchActiveIndex((current) => (current + delta + options.length) % options.length);
      return;
    }
    if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
    const choice = options[searchActiveIndex] ?? options[0];
    if (!choice) return;
    if (choice.kind === "stock") openStockDetail(choice.item as StockSearchItem);
    else openEtfFromSearch(choice.item as EtfTerminalInstrument);
  };
  const dateIndex = availableDates.indexOf(snapshot.asOf);
  const previousAvailableDate = dateIndex > 0 ? availableDates[dateIndex - 1] : null;
  const nextAvailableDate = dateIndex >= 0 && dateIndex < availableDates.length - 1
    ? availableDates[dateIndex + 1]
    : null;
  const openDate = (date: string) => {
    if (!availableDates.includes(date)) {
      setNotice("该日期尚未形成完整发布版本，请选择已归档交易日");
      window.setTimeout(() => setNotice(""), 2400);
      return;
    }
    const url = new URL(window.location.href);
    // “最新” must use the active release. It contains the complete
    // materialized ETF state; the historical archive is intentionally a
    // compact snapshot and may only contain the 1d terminal period.
    const latest = availableDates.at(-1);
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
      if (document.visibilityState === "visible") {
        // Record the check before starting the request.  Otherwise a tab that
        // regains focus immediately after the interval fires can schedule a
        // second pointer request for the same check window.
        lastCheck = Date.now();
        if (onAutoRefresh) void onAutoRefresh();
      }
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
  }, [autoRefresh, onAutoRefresh, requestedDate]);

  const choosePeriod = (next: FlowPeriod) => {
    if (!activeAvailablePeriods.has(next)) {
      setNotice(`${periods.find((item) => item.id === next)?.label}当前版本未声明此观察窗口`);
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
            return <button aria-current={view === item.id ? "page" : undefined} aria-label={item.label} className={view === item.id ? "active" : ""} key={item.id} onClick={() => navigateView(item.id)} title={item.label} type="button"><Icon size={17} /><span>{item.label}</span></button>;
          })}
        </nav>
        <div className="top-actions">
          <div className="global-search-shell">
            <label className="global-search"><MagnifyingGlass size={18} /><input aria-label="搜索 ETF、个股或代码" onChange={(event) => { const value = event.target.value; setGlobalQuery(value); setSearchActiveIndex(0); if (view === "national" || view === "radar") setQuery(value); }} onKeyDown={handleGlobalSearchKeyDown} placeholder="搜索 ETF / 个股 / 代码，回车查看" value={globalQuery} /></label>
            {globalQuery.trim() && (
              <div className="global-search-results" role="listbox" aria-label="搜索结果">
                {globalSearchResults.stocks.length ? <section><header>个股</header>{globalSearchResults.stocks.map((item, index) => <button aria-selected={searchActiveIndex === index} className={searchActiveIndex === index ? "is-active" : ""} key={`${item.listingMarket}:${item.code}`} onClick={() => openStockDetail(item)} role="option" type="button"><span><strong>{item.name}</strong><small>{item.code} · {item.listingMarketName ?? item.listingMarket}</small></span><em>{item.available ? "个股" : "雷达兼容"}</em></button>)}</section> : null}
                {globalSearchResults.etfs.length ? <section><header>ETF</header>{globalSearchResults.etfs.map((item, index) => { const optionIndex = globalSearchResults.stocks.length + index; return <button aria-selected={searchActiveIndex === optionIndex} className={searchActiveIndex === optionIndex ? "is-active" : ""} key={`${item.market}:${item.code}`} onClick={() => openEtfFromSearch(item)} role="option" type="button"><span><strong>{item.name}</strong><small>{item.code} · {item.market === "SZSE" ? "深市" : "沪市"}</small></span><em>ETF</em></button>; })}</section> : null}
                {!globalSearchResults.stocks.length && !globalSearchResults.etfs.length && <span className="global-search-no-result">{stockSearchLoading ? "正在读取当前发布的个股索引…" : stockSearchError === "release_pending" ? "新版本仍在发布，暂不拼接不同版本搜索索引" : stockSearchError ? "当前发布暂不可用个股索引" : "没有找到匹配的 ETF 或个股"}</span>}
              </div>
            )}
          </div>
          <button className="icon-button" title="打印或保存当前页面" onClick={() => window.print()} type="button"><Camera size={19} /></button>
          <button className={`refresh-toggle ${autoRefresh && !requestedDate ? "enabled" : ""}`} aria-pressed={autoRefresh && !requestedDate} disabled={Boolean(requestedDate) || refreshing} onClick={() => setAutoRefresh((value) => !value)} title={requestedDate ? "历史归档不会自动切换日期" : "每 30 分钟检查最新发布数据"} type="button"><span /><em>{refreshing ? "检查中" : "自动更新"}</em></button>
          <button className={`icon-button ${refreshing ? "is-refreshing" : ""}`} title="刷新数据" aria-label="刷新数据" disabled={refreshing} onClick={() => void onRefresh?.()} type="button"><ArrowsClockwise size={19} /></button>
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

      <section className={`data-status-strip is-${dataStatus.tone}`} role="status" aria-live="polite">
        <span className="data-status-dot" aria-hidden="true" />
        <strong>{dataStatus.title}</strong>
        <span>{dataStatus.detail}</span>
        <small>{releaseId ? `release ${releaseId.slice(0, 10)}` : `截至 ${snapshot.asOf}`}</small>
      </section>

      {(view === "overview" || view === "rotation" || view === "radar" || view === "national") && (
        <section className="period-strip" aria-label="ETF 观察口径">
          {view !== "national" && <div className="metric-switch" aria-label="指标口径">
            <button className={etfDisplayMetric === "shares" ? "active" : ""} onClick={() => setEtfDisplayMetric("shares")} type="button">份额（亿份）</button>
            <button className={etfDisplayMetric === "cash" ? "active" : ""} disabled={!cashAvailable} onClick={() => setEtfDisplayMetric("cash")} title={cashAvailable ? "逐日份额变化 × 对应交易日官方单位净值，单位亿元" : terminal.cashFlow?.warning ?? "等待官方单位净值覆盖"} type="button">金额估算（亿元）</button>
          </div>}
          <div className="period-tabs" aria-label="观察周期">
            {periods.map((item) => {
              const selectable = activeSelectablePeriods.has(item.id);
              const covered = metricAvailablePeriods.has(item.id);
              return <button aria-pressed={period === item.id} aria-disabled={!selectable} className={`${period === item.id ? "active" : ""} ${covered ? "" : "unavailable"}`} disabled={!activeAvailablePeriods.has(item.id)} key={item.id} onClick={() => choosePeriod(item.id)} title={!selectable ? "当前版本未声明此观察窗口" : covered ? undefined : "可查看，但当前没有完整官方交易日覆盖"} type="button">{item.label}</button>;
            })}
          </div>
          <div className="coverage-chip"><Database size={16} /><span>{activeSelectablePeriods.size} 个可查周期</span><strong>{metricAvailablePeriods.size} 个有覆盖 · {view === "national" ? `${nationalTeamEtfs.length} 只观察池 ETF` : etfDisplayMetric === "cash" ? `${terminal.cashFlow?.eligibleInstruments ?? 0} 只净值可比` : `${terminal.universe?.total ?? terminal.instruments.length} 只 ETF`} · 沪 {terminal.universe?.marketCounts.SSE ?? terminal.instruments.filter((item) => item.market !== "SZSE").length} / 深 {terminal.universe?.marketCounts.SZSE ?? terminal.instruments.filter((item) => item.market === "SZSE").length}</strong></div>
        </section>
      )}

      {(view === "overview" || view === "rotation" || view === "radar") && (
        <section className="etf-reconciliation-strip" aria-label="ETF 样本对账">
          <div className="reconciliation-title"><span className="eyebrow">同口径校验</span><strong>{terminal.scope ?? "SSE+SZSE"} · {currentPeriod}</strong></div>
          <div className="reconciliation-stats">
            <span><small>可比样本</small><strong>{selectedPeriodCovered ? reconciliation.comparable : "—"}<em>{selectedPeriodCovered ? "只" : ""}</em></strong></span>
            <span><small>增加 / 减少 / 不变</small><strong className="reconciliation-counts">{selectedPeriodCovered ? <><b className="text-in">{reconciliation.increased}</b><b className="text-out">{reconciliation.decreased}</b><b>{reconciliation.unchanged}</b><em>只</em></> : "—"}</strong></span>
            <span><small>已归类 / 未归类</small><strong>{selectedPeriodCovered ? <>{reconciliation.classifiedTotal}<b className="text-amber"> / {reconciliation.unclassifiedTotal}</b><em>只</em></> : "—"}</strong></span>
            <span><small>ETF 合计</small><strong className={!selectedPeriodCovered ? "" : reconciliation.instrumentTotal >= 0 ? "text-in" : "text-out"}>{selectedPeriodCovered ? formatFlow(reconciliation.instrumentTotal, 2) : "—"}<em>{selectedPeriodCovered ? metricUnit : ""}</em></strong></span>
            <span><small>板块合计 · 对账差异</small><strong>{selectedPeriodCovered ? formatFlow(reconciliation.groupTotal, 2) : "—"}{selectedPeriodCovered ? <><b className={Math.abs(reconciliation.delta) < 0.01 ? "text-in" : "text-amber"}> · {formatFlow(reconciliation.delta, 2)}</b><em>{metricUnit}</em></> : null}</strong></span>
          </div>
          <p>{metricAvailablePeriods.has(period) ? "板块合计 = ETF 合计 + 对账差异；未归类产品保留在“未归类”桶内，不强行归入行业。" : "当前观察窗口尚无完整官方交易日覆盖，数值保持为空，不用前值或估算值补齐。"}{etfDisplayMetric === "cash" ? "金额为逐日份额变化 × 对应交易日官方单位净值估算。" : "当前为官方份额变化，不等同现金净流入。"}</p>
        </section>
      )}

      {notice && <div className="toast" role="status"><Info size={17} />{notice}</div>}

      <div className="desk-content">
        {viewLoading && <div className="view-loading" aria-busy="true" role="status"><ArrowsClockwise className="is-refreshing" size={18} />正在读取当前视图数据…</div>}
        {view === "overview" && (
          <>
            <section className="overview-grid">
              <article className="terminal-panel rotation-panel">
                <div className="panel-heading">
                  <div><span className="eyebrow">ETF 版本化主分类 · 公式计算</span><h2>{`板块${metricLabel}`}</h2><p>{terminal.classificationVersion ?? "未标版本"} · {etfDisplayMetric === "cash" ? "逐日份额变化按对应交易日官方单位净值折算后累计" : terminal.calculation ?? `当前展示 ${currentPeriod} 官方份额差`}</p></div>
                  <div className="panel-kpis panel-kpis-with-link"><span><small>样本净变化（{metricUnit}）</small><strong className={totalGroupFlow === null ? "" : totalGroupFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(totalGroupFlow, 1)}</strong></span><span><small>拥挤度</small><strong className="text-amber">{metricAvailablePeriods.has(period) ? `${crowding}%` : "—"}</strong></span><button className="link-button" onClick={() => navigateView("rotation")} type="button">展开<CaretRight size={15} /></button></div>
                </div>
                {metricAvailablePeriods.has(period) ? <ReactECharts className="rotation-chart" option={rotationOption} notMerge lazyUpdate /> : <div className="empty-chart"><Database size={30} /><strong>当前周期暂无完整官方覆盖</strong><span>可以继续查看此周期；有真实快照后自动显示，不补造数值</span></div>}
                <div className="insight-row">
                  <span><i>01</i><p><strong>{topGroup?.name ?? "暂无可比数据"}{topGroup ? "居首" : ""}</strong>{topGroup ? `可见样本中${metricLabel}最显著。` : "当前周期暂无可比板块。"}</p></span>
                  <span><i>02</i><p><strong>集中度 {selectedPeriodCovered ? `${crowding}%` : "—"}</strong>{selectedPeriodCovered ? "前三流入方向占可见流入样本。" : "当前周期暂无完整官方覆盖。"}</p></span>
                  <span><i>03</i><p><strong>口径可审计</strong>官方类型优先，低置信度产品进入“未归类”并纳入对账。</p></span>
                </div>
              </article>

              <article className="terminal-panel trend-panel">
                <div className="panel-heading">
                  <div><span className="eyebrow">全市场 ETF</span><h2>净{metricLabel}走势</h2><p>{etfDisplayMetric === "cash" ? "逐日份额变化 × 对应交易日官方单位净值；缺失净值不计入" : "相邻官方交易日份额差；正值为增加"}</p></div>
                  <button className="link-button" onClick={() => navigateView("radar")} type="button">打开雷达<CaretRight size={15} /></button>
                </div>
                {terminal.dailySeries.length ? <ReactECharts className="trend-chart" option={trendOption} notMerge lazyUpdate /> : <div className="empty-chart"><Database size={30} /><strong>等待第二个交易日快照</strong><span>有连续数据后自动生成走势</span></div>}
                <div className="trend-summary">
                  <span><small>当日</small><strong className={latestDisplayValue === null ? "" : latestDisplayValue >= 0 ? "text-in" : "text-out"}>{latestDisplayText}<em>{metricUnit}</em></strong></span>
                  <span><small>覆盖</small><strong>{(etfDisplayMetric === "cash" ? latestCashCoverage : terminal.dailySeries.at(-1)?.coverage) ?? 0}<em>{etfDisplayMetric === "cash" ? "只净值" : "只"}</em></strong></span>
                  <span><small>当前方向</small><strong className="text-cyan">{latestDisplayDirection}</strong></span>
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

        {view === "pressure" && (
          <EtfPressureView data={snapshot.pressure} onOpenStock={openStockFromFlow} />
        )}

        {view === "rotation" && (
          <SectorRotationView
            asOf={snapshot.asOf}
            crowding={crowding}
            dailySeries={terminal.dailySeries}
            metricLabel={metricLabel}
            metricUnit={metricUnit}
            onBack={() => navigateView("overview")}
            onOpenRadar={() => navigateView("radar")}
            periodLabel={currentPeriod}
            rows={groupRows}
            totalFlow={totalGroupFlow}
          />
        )}

        {view === "radar" && (
          <section className="terminal-panel radar-panel">
            <div className="radar-heading">
              <div><span className="eyebrow">ETF 雷达 · 公式计算</span><h2>横向比较{metricLabel}</h2><p>{terminal.universe?.total ?? terminal.instruments.length} 只 ETF · 红色增加，绿色减少 · 默认按绝对变化排序 · {etfDisplayMetric === "cash" ? "官方单位净值覆盖不足时保持为空" : "连续官方份额不足时保持为空"}</p></div>
              <div className="radar-stats"><span><small>当前筛选净变化</small><strong className={filteredTotalFlow === null ? "" : filteredTotalFlow >= 0 ? "text-in" : "text-out"}>{formatFlow(filteredTotalFlow, 1)}<em>{filteredTotalFlow === null ? "" : metricUnit}</em></strong></span><span><small>可比样本</small><strong className="text-cyan">{metricComparableCount}<em>只</em></strong></span></div>
            </div>
            <div className="radar-toolbar">
              <div className="radar-filters">
                <div className="direction-tabs">{(["all", "inflow", "outflow"] as DirectionFilter[]).map((item) => <button className={direction === item ? "active" : ""} key={item} onClick={() => setDirection(item)} type="button">{item === "all" ? "全部" : item === "inflow" ? `${metricLabel}增加` : `${metricLabel}减少`}</button>)}</div>
                <div className="market-tabs">{(["all", "SSE", "SZSE"] as MarketFilter[]).map((item) => <button className={marketFilter === item ? "active" : ""} key={item} onClick={() => setMarketFilter(item)} type="button">{item === "all" ? "沪深" : item === "SSE" ? "沪市" : "深市"}</button>)}</div>
                <label className="category-select"><span>主分类</span><select aria-label="ETF 主分类" onChange={(event) => setCategoryFilter(event.target.value)} value={categoryFilter}><option value="all">全部分类</option>{categories.map((category) => <option key={category} value={category}>{category} ({terminal.universe?.categoryCounts[category] ?? terminal.instruments.filter((item) => item.category === category).length})</option>)}</select></label>
              </div>
              <span className="sort-status"><ArrowsLeftRight size={16} />当前排序：{periods.find((item) => item.id === (sorting[0]?.id ?? period))?.label ?? currentPeriod} · {periods.some((item) => item.id === sorting[0]?.id) ? `绝对变化${sorting[0]?.desc ? "降序" : "升序"}` : sorting[0]?.desc ? "降序" : "升序"}</span>
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
              <div className="panel-heading connect-heading"><div className="connect-heading-copy"><span className="eyebrow">沪港通 · 深港通</span><h2>互联互通数据</h2><p>南向看公式净买入；北向只看官方成交活跃度 · {marketHistoryCoverage?.connect.points ?? connectSeries.length} 个官方历史点{marketHistoryCoverage?.connect.from ? `（${marketHistoryCoverage.connect.from} 至 ${marketHistoryCoverage.connect.to}）` : ""}</p></div><p className="connect-heading-note"><span className="connect-heading-note-badge">当日四通道</span><span className="connect-heading-note-copy">南向看方向 · 北向看活跃度</span></p></div>
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
              <div className="panel-heading"><div><span className="eyebrow">{marginHasBse ? "沪深北三市场" : "沪深两市"} · 官方汇总</span><h2>两融余额与交易数据</h2><p>{marginMetricDefinitions[marginDisplayMetric].note}；{marketHistoryCoverage?.margin.points ?? snapshot.marginSeries.length} 个官方历史点{marketHistoryCoverage?.margin.from ? `（${marketHistoryCoverage.margin.from} 至 ${marketHistoryCoverage.margin.to}）` : ""}</p></div><div className="margin-hero-value"><small>最新完整数据日 · {snapshot.asOf}</small><strong className="hero-number">{marginMetric?.value}<em>{marginMetric?.unit}</em></strong></div></div>
              <div className="margin-metric-tabs">{(Object.keys(marginMetricDefinitions) as MarginDisplayMetric[]).map((item) => <button className={marginDisplayMetric === item ? "active" : ""} key={item} onClick={() => setMarginDisplayMetric(item)} type="button">{marginMetricDefinitions[item].label}</button>)}</div>
              <ReactECharts className="margin-detail-chart" option={marginTrendOption} notMerge lazyUpdate />
              <div className="margin-kpi-strip"><div><span>最新融资买入</span><strong className="text-in">{formatFlow(snapshot.marginSeries.at(-1)?.financingBuy)}<em>亿元</em></strong></div><div><span>最新融券余额</span><strong className="text-amber">{formatFlow(snapshot.marginSeries.at(-1)?.lendingBalance)}<em>亿元</em></strong></div><div><span>上交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.sseBalance)}<em>亿元</em></strong></div><div><span>深交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.szseBalance)}<em>亿元</em></strong></div>{marginHasBse ? <div><span>北交所融资余额</span><strong>{formatFlow(snapshot.marginSeries.at(-1)?.bseBalance)}<em>亿元</em></strong></div> : null}</div>
              <section className="margin-insights" aria-labelledby="margin-insights-title"><div className="margin-insights-heading"><div><span className="eyebrow">从原始序列复算</span><h3 id="margin-insights-title">两融状态解读</h3></div><p>余额看存量趋势，买入额看交易活跃度；两者不能互相替代。</p></div><div className="margin-insight-grid">{marginInsights.map((item) => <article key={item.label}><header><span>{item.label}</span><small>{item.status}</small></header><strong className={item.tone}>{item.value}<em>{item.unit}</em></strong><p>{item.note}</p></article>)}</div></section>
            </article>
            <aside className="terminal-panel methodology"><Pulse size={24} /><h3>如何阅读</h3><p>{marginMetric?.delta}。余额变化只说明杠杆存量扩张或收缩，需要与指数趋势和成交额一起观察。</p><span>官方汇总 · 最新 {snapshot.asOf}</span><div className="method-list"><p><b>融资余额</b>存量杠杆规模，不叫净流入</p><p><b>融资买入</b>当日融资买入成交额</p><p><b>融券余额</b>融券卖出尚未偿还金额</p></div></aside>
          </section>
        )}
      </div>

      <footer className="terminal-footer"><span><i className="status-light" />{snapshot.publication?.status === "confirmed" ? "完整交易日已确认" : snapshot.gate === "conditional_pass" ? "条件通过" : snapshot.gate} · 观测于 {snapshot.observedAt}</span><span>{etfDisplayMetric === "cash" ? "金额为逐日官方份额变化 × 对应交易日官方单位净值的估算，不等同真实申赎现金" : "份额变化不等同资金金额"} · 仅供数据观察，不构成投资建议</span><a href="/api/v1/data-status">机器可读状态</a></footer>

      {selected && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelected(null)}>
          <aside aria-label={`${selected.name}详情`} aria-modal="true" className="detail-drawer" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <div className="drawer-header"><div><span className="eyebrow">ETF 详情</span><h2>{selected.name}</h2><p>{selected.code} · {selected.market === "SZSE" ? "深交所" : "上交所"} · {selected.category} · {groupTypeLabels[selected.groupType ?? "other"] ?? selected.groupType} · {selected.assetClass ?? "未分类"}</p></div><button aria-label="关闭详情" className="icon-button" onClick={() => setSelected(null)} ref={closeButtonRef} type="button"><X size={19} /></button></div>
            <div className="drawer-hero"><span>{currentPeriod}{metricLabel} · {etfDisplayMetric === "cash" ? "对应交易日官方单位净值估算" : "官方份额差"}</span><strong className={(selectedActiveValue ?? 0) >= 0 ? "text-in" : "text-out"}>{formatFlow(selectedActiveValue)}<em>{metricUnit}</em></strong><small>{etfDisplayMetric === "cash" ? `${selected.cashPeriodCoverage?.[period] ?? 0} 个官方单位净值覆盖 · 不等同真实申赎现金` : selectedHistory.length >= 2 ? `基于 ${selectedHistory.length} 个原始官方份额日点` : `覆盖 ${selected.periodCoverage?.[period] ?? selected.coverage} 个官方交易日变化值 · 不等同现金净流入`}</small></div>
            <div className="drawer-proof-grid" aria-label="ETF 数据凭据"><div><span>展示快照日</span><strong>{snapshot.asOf}</strong><small>当前归档日</small></div><div><span>原始来源</span><strong>{selected.market === "SZSE" ? "深交所" : "上交所"}</strong><small>ETF 规模文件</small></div><div><span>当前总份额</span><strong>{typeof selected.shares === "number" ? selected.shares.toFixed(2) : "待快照"}</strong><small>亿份</small></div></div>
            <div className="drawer-share-chart">
              <div><strong>官方份额走势</strong><span>{selectedHistoryLoading ? "正在读取官方历史…" : selectedHistory.length >= 2 ? `${selectedHistoryBasis === "official_daily_change_replay" ? "官方变化回溯" : "原始日点"} · ${selectedHistory[0].date} 至 ${selectedHistory.at(-1)?.date}` : "复算锚点 · 等待原始日点"}</span></div>
              {selectedShareHistory.length >= 2 ? (
                <ReactECharts className="drawer-share-echart" option={selectedShareChartOption} notMerge lazyUpdate />
              ) : (
                <div className="drawer-chart-empty"><Database size={22} /><strong>{selectedHistoryLoading ? "正在读取原始份额" : typeof selected.shares === "number" ? "历史节点积累中" : "等待官方总份额快照"}</strong><span>{selectedHistoryLoading ? "详情页正在按基金代码读取交易所归档数据。" : typeof selected.shares === "number" ? `当前只有 ${selectedShareHistory.length} 个可复算节点，达到 2 个节点后显示走势。` : "拿到官方总份额后，才会显示可验证的历史节点。"}</span></div>
              )}
              <p>{selectedHistory.length >= 2 ? (selectedHistoryBasis === "official_daily_change_replay" ? "折线由官方逐日份额变化回溯到当前总份额；下方周期值不是现金净流入。" : "折线为交易所归档的官方总份额日点；下方周期值为当前份额减去对应历史日点，不是现金净流入。") : "在原始日点接口可用前，历史节点由当前官方总份额减去周期份额变化复算；复算锚点，不是原始逐日快照，也不是现金净流入。"}</p>
            </div>
            <div className="drawer-periods">{selectedAvailablePeriods.map((item) => <div className={item.id === period ? "active" : ""} key={item.id}><span>{item.label}</span><strong className={(item.value ?? 0) > 0 ? "text-in" : (item.value ?? 0) < 0 ? "text-out" : ""}>{formatFlow(item.value)}</strong><small>{metricUnit}</small></div>)}</div>
            {selectedPendingPeriods.length ? <details className="drawer-periods-more"><summary>历史周期积累中 · {selectedPendingPeriods.length} 项</summary><div className="drawer-pending-periods">{selectedPendingPeriods.map((item) => <span key={item.id}>{item.label}<small>待积累</small></span>)}</div></details> : null}
            {radarFlowLoading && !selectedStockFlow ? <section className="drawer-stock-flow" aria-busy="true"><Database size={18} /><span>正在读取 ETF→个股穿透数据…</span></section> : null}
            {radarFlowError && !selectedStockFlow ? <section className="drawer-stock-flow drawer-stock-flow-empty" role="alert"><Database size={18} /><span>ETF→个股穿透数据暂时不可用，未影响 ETF 份额数据。</span></section> : null}
            {selectedStockFlow ? <section className="drawer-stock-flow" aria-label="ETF个股穿透估算"><header><div><span className="eyebrow">ETF → 个股</span><strong>申赎篮子等价变化</strong></div><span className={`evidence-badge ${selectedStockFlow.flow.available && selectedStockFlow.flow.basis.pcfAlignment !== "next_day_previous_trade_date" ? "evidence-calculated" : "evidence-inferred"}`}>{selectedStockFlow.flow.available ? (selectedStockFlow.flow.basis.pcfAlignment === "next_day_previous_trade_date" ? "次日 PCF" : "同日 PCF") : "暂不可用"}</span></header>{selectedStockFlow.flow.available ? <><p className="drawer-stock-flow-basis">份额日 {selectedStockFlow.flow.basis.shareDate} · PCF 发布日 {selectedStockFlow.flow.basis.pcfDate ?? "—"} · {selectedStockFlow.flow.basis.pcfAlignment === "next_day_previous_trade_date" ? "按 PRE_TRADING_DAY 对齐" : "同日对齐"} · 仅估算申赎篮子，不等同二级市场现金流。</p>{!selectedStockFlow.covered ? <div className="drawer-stock-flow-empty"><Database size={18} /><span>{selected.market === "SZSE" ? "当前只归档上交所 PCF，深交所 ETF 暂不估算。" : "该 ETF 未进入 PCF 覆盖，暂不估算。"}</span></div> : selectedStockFlow.rows.length ? <div className="drawer-stock-flow-list">{selectedStockFlow.rows.slice(0, 8).map((row) => <button className="drawer-stock-flow-row" key={`${row.market}:${row.stockCode}`} onClick={() => openStockFromFlow(row)} type="button"><span><b>{row.stockName}</b><small>{row.stockCode} · {row.listingMarketName ?? row.market}</small></span><strong className={row.value >= 0 ? "text-in" : "text-out"}>{formatShares(row.value)}</strong><CaretRight size={14} /></button>)}</div> : <div className="drawer-stock-flow-empty"><Database size={18} /><span>该 ETF 当日没有可展示的个股贡献。</span></div>}<small className="drawer-stock-flow-foot">点击个股查看完整贡献拆解 · 覆盖 {selectedStockFlow.flow.coverage.matchedEtfCount} 只 ETF · {selectedStockFlow.flow.coverage.stockCount} 只个股{selectedStockFlow.flow.coverage.limited ? " · 已按上限截取" : ""}</small>{selectedStockFlow.flow.coverage.captureWarnings?.length && selectedStockFlow.flow.basis.pcfAlignment !== "next_day_previous_trade_date" ? <small className="drawer-stock-flow-foot">PCF 覆盖提示：当前结果存在部分采集告警，未将缺失数据用其他日期补齐。</small> : null}</> : <><p className="drawer-stock-flow-basis">{selectedStockFlow.flow.warnings[0] === "pcf_date_mismatch" ? "交易所 PCF 与份额日期不一致，系统不会用其他日期替代。" : "官方 PCF 尚未归档，暂不估算个股穿透。"}</p><small className="drawer-stock-flow-foot">需同时具备官方 ETF 份额快照与 PCF；PCF 可按 PRE_TRADING_DAY 与份额日对齐。</small></>}</section> : null}
            <div className="drawer-note"><Info size={18} /><p><strong>分类与口径</strong>{classificationRuleLabels[selected.classificationRule ?? ""] ?? "未记录规则"} · 置信度 {confidenceLabels[selected.classificationConfidence ?? ""] ?? "未知"}。{etfDisplayMetric === "cash" ? "金额估算按每日官方份额变化乘对应交易日官方单位净值后累加；缺少净值的日期不补造。" : "当前指标是 ETF 份额变化；长周期为每日官方份额变化逐日累计，缺少历史覆盖时保持为空，不补造。"}</p></div>
          </aside>
        </div>
      )}
    </main>
  );
}
