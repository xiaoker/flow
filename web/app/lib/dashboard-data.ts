export type FlowDirection = "inflow" | "outflow";

export type FlowTone = "positive" | "negative" | "neutral";

export type EvidenceKind = "official" | "calculated" | "inferred";

export type FlowPeriod = "1d" | "3d" | "1w" | "2w" | "1m" | "3m" | "6m" | "ytd" | "12m";

export interface EtfTerminalInstrument {
  code: string;
  name: string;
  market?: "SSE" | "SZSE";
  category: string;
  groupType?: string;
  assetClass?: string;
  classificationSource?: string;
  classificationConfidence?: string;
  classificationRule?: string;
  includeInGroup?: boolean;
  shares: number | null;
  nav?: number | null;
  navDate?: string | null;
  periods: Partial<Record<FlowPeriod, number | null>>;
  estimatedCashPeriods?: Partial<Record<FlowPeriod, number | null>>;
  coverage: number;
  periodCoverage?: Partial<Record<FlowPeriod, number>>;
  cashPeriodCoverage?: Partial<Record<FlowPeriod, number>>;
}

export interface EtfTerminalData {
  /** The radar/rotation display universe. Current scope is both exchanges. */
  scope?: "SSE+SZSE" | string;
  dailySeries: Array<{
    date: string;
    value: number;
    coverage: number;
    cashValue?: number | null;
    cashCoverage?: number;
  }>;
  instruments: EtfTerminalInstrument[];
  groupPeriods: Array<{
    name: string;
    groupType?: string;
    periods: Partial<Record<FlowPeriod, number | null>>;
    estimatedCashPeriods?: Partial<Record<FlowPeriod, number | null>>;
    estimatedCashCoverage?: Partial<Record<FlowPeriod, number>>;
    componentCount?: number;
    comparableCount?: Partial<Record<FlowPeriod, number>>;
  }>;
  availablePeriods?: FlowPeriod[];
  classification?: string;
  classificationVersion?: string;
  calculation?: string;
  cashFlow?: {
    available: boolean;
    unit: "亿元" | string;
    method: string;
    eligibleInstruments: number;
    totalInstruments: number;
    coverageDate?: string;
    warning?: string;
  };
  historyCoverage?: {
    from: string;
    to: string;
    tradingDays: number;
    stateVersion: number;
  };
  /** Periods with at least one official NAV observation for cash estimates. */
  cashAvailablePeriods?: FlowPeriod[];
  universe?: {
    total: number;
    excluded?: number;
    exclusionCounts?: Record<string, number>;
    marketCounts: Record<string, number>;
    categoryCounts: Record<string, number>;
    classified?: number;
    unclassified?: number;
    periodComparable: number;
    cashFlowEligible?: number;
  };
  reconciliation?: Record<string, {
    shares?: {
      comparable: number;
      increased: number;
      decreased: number;
      unchanged: number;
      classified: number;
      unclassified: number;
      instrumentTotal: number;
      groupTotal: number;
      delta: number;
    };
    cash?: {
      comparable: number;
      increased: number;
      decreased: number;
      unchanged: number;
      classified: number;
      unclassified: number;
      instrumentTotal: number;
      groupTotal: number;
      delta: number;
    };
  }>;
}

export interface EtfStockFlowContributor {
  etfCode: string;
  etfName: string;
  shareDelta: number;
  quantity: number;
  creationUnit: number;
  stockFlowShares: number;
  currentPenetratedShares: number;
  estimated: boolean;
}

export interface EtfStockFlowStock {
  market: "SSE" | "SZSE" | string;
  listingMarket?: string;
  listingMarketName?: string;
  stockCode: string;
  stockName: string;
  netShares: number;
  inflowShares: number;
  outflowShares: number;
  penetratedShares?: number | null;
  etfCount: number;
  contributors: EtfStockFlowContributor[];
}

export interface EtfStockFlowData {
  available: boolean;
  unit: string;
  asOf: string;
  basis: {
    shareDate: string;
    previousShareDate: string | null;
    pcfDate: string | null;
    pcfAlignment?: "same_trading_day" | "next_day_previous_trade_date" | "unavailable" | string;
    timezone: string;
    calculation: string;
    source: string;
    cashSubstitution?: string;
  };
  coverage: {
    shareEtfCount: number;
    pcfEtfCount: number;
    matchedEtfCount: number;
    stockCount: number;
    shownStockCount: number;
    limited: boolean;
    netSharesTotal?: number;
    shownNetShares?: number;
    captureWarnings?: string[];
  };
  stocks: EtfStockFlowStock[];
  etfs: Array<{
    market: "SSE" | "SZSE" | string;
    code: string;
    name: string;
    shareDelta: number;
    creationUnit: number;
    stockCount: number;
    stockShownCount?: number;
    stocks?: Array<{
      market: "SSE" | "SZSE" | string;
      listingMarket?: string;
      listingMarketName?: string;
      stockCode: string;
      stockName: string;
      stockFlowShares: number;
      currentPenetratedShares: number;
      quantity: number;
      estimated: boolean;
    }>;
    estimated: boolean;
  }>;
  warnings: string[];
}

export interface StockPressureHistoryPoint {
  date: string;
  netShares: number;
  inflowShares: number;
  outflowShares: number;
  etfCount: number;
  penetratedShares?: number | null;
}

export interface StockPressureContributor {
  etfMarket?: string;
  etfCode: string;
  etfName: string;
  shareDelta: number;
  quantity: number;
  creationUnit: number;
  stockFlowShares: number;
  currentPenetratedShares?: number;
  cashSubstitution?: boolean;
  estimated: boolean;
}

export interface StockPressureRow {
  identity: string;
  listingMarket: string;
  listingMarketName: string;
  stockCode: string;
  stockName: string;
  shard: string;
  netShares: number;
  inflowShares: number;
  outflowShares: number;
  penetratedShares?: number | null;
  etfCount: number;
  historyCoverage: number;
  history?: StockPressureHistoryPoint[];
  rolling20: {
    from: string;
    to: string;
    coverage: number;
    netShares: number;
    inflowShares: number;
    outflowShares: number;
  } | null;
  contributors?: StockPressureContributor[];
  closePriceYuan?: number | null;
  floatShares?: number | null;
  floatMarketCapYuan?: number | null;
  marginBalanceYuan?: number | null;
  shortBalanceYuan?: number | null;
  financingBuyYuan?: number | null;
  financingRepaymentYuan?: number | null;
  shortBalanceShares?: number | null;
  shortSellShares?: number | null;
  shortRepaymentShares?: number | null;
  priceAsOf?: string | null;
  marginAsOf?: string | null;
  marginDataSource?: string | null;
}

export interface EtfPressureData {
  schemaVersion: number;
  asOf: string;
  basis: {
    unit: string;
    calculation: string;
    identity: string;
    interpretation: string;
    source: string;
    priceSource: string;
  };
  summary: {
    netShares: number;
    inflowShares: number;
    outflowShares: number;
    grossShares: number;
    marketFinancingBalanceYuan: number | null;
  };
  coverage: {
    stockCount: number;
    matchedEtfCount: number;
    history20StockCount: number;
    history20Coverage: number;
    priceCount: number;
    marginCount: number;
    securityMarginCoverage?: {
      asOf?: string;
      available?: boolean;
      source?: string | null;
      matchedCount?: number;
      totalCount?: number;
      availableMarkets?: string[];
      unsupportedMarkets?: string[];
      missingReason?: string | null;
    } | null;
  };
  availableDates: string[];
  marginAsOf?: string | null;
  stocks: StockPressureRow[];
  warnings: string[];
}

export interface StockSearchItem {
  listingMarket: string;
  listingMarketName?: string;
  code: string;
  name: string;
  shard: string;
  available: boolean;
}

export interface StockFlowDetailContributor {
  etfMarket?: string;
  etfCode: string;
  etfName: string;
  shareDelta: number;
  quantity: number;
  creationUnit: number;
  stockFlowShares: number;
  currentPenetratedShares?: number;
  cashSubstitution?: boolean;
  estimated: boolean;
}

export interface StockFlowDetail {
  listingMarket: string;
  listingMarketName: string;
  stockCode: string;
  stockName: string;
  netShares: number;
  inflowShares: number;
  outflowShares: number;
  etfCount: number;
  contributorCount: number;
  shownContributorCount: number;
  contributorsLimited: boolean;
  contributors: StockFlowDetailContributor[];
  estimated?: boolean;
}

export interface DashboardSnapshot {
  asOf: string;
  observedAt: string;
  mode: string;
  gate: string;
  quality: { pass: number; warn: number; fail: number };
  publication?: {
    status: "confirmed" | "pending";
    targetAsOf: string;
    pendingDomains: string[];
    detail: string;
  };
  metrics: Array<{
    id: string;
    eyebrow: string;
    value: string;
    unit: string;
    delta: string;
    tone: FlowTone;
    note: string;
    basis?: {
      kind: EvidenceKind;
      label: string;
      detail: string;
    };
  }>;
  etfFlows: Record<FlowDirection, Array<{ code: string; name: string; value: number }>>;
  marginSeries: Array<{
    date: string;
    balance: number;
    label: string;
    change?: number | null;
    financingBuy?: number;
    lendingBalance?: number;
    lendingSellVolume?: number;
    totalBalance?: number;
    sseBalance?: number;
    szseBalance?: number;
    bseBalance?: number;
    marginScopeVersion?: "sse_szse_v1" | "sse_szse_bse_v2" | string;
  }>;
  connect: Array<{
    name: string;
    value: string;
    unit: string;
    description: string;
    tone: FlowTone;
  }>;
  connectSeries?: Array<{
    date: string;
    southboundNet: number;
    southboundTurnover: number;
    southboundSseNet: number;
    southboundSzseNet: number;
    northboundTurnover: number;
    northboundEtfTurnover: number;
    northboundTradeCount: number;
  }>;
  marketHistoryCoverage?: {
    archiveFrom: string;
    archiveTo: string;
    archiveDates: number;
    margin: { from: string | null; to: string | null; points: number };
    connect: { from: string | null; to: string | null; points: number };
  };
  dataStatus: Array<{
    name: string;
    status: "ready" | "warning";
    asOf: string;
    detail: string;
    basis?: "official" | "inferred_date" | "mixed";
  }>;
  etfTerminal?: EtfTerminalData;
  etfSearch?: Array<{
    code: string;
    name: string;
    market?: string;
    category?: string;
  }>;
  etfStockFlow?: EtfStockFlowData;
  pressure?: EtfPressureData;
}

export const dashboardSnapshot: DashboardSnapshot = {
  asOf: "2026-07-14",
  observedAt: "2026-07-16 00:33 CST",
  mode: "Phase 0 本地实数快照",
  gate: "conditional_pass",
  quality: { pass: 46, warn: 2, fail: 0 },
  metrics: [
    {
      id: "etf",
      eyebrow: "沪深 ETF 份额变化",
      value: "+79.89",
      unit: "亿份",
      delta: "299 只增加 · 229 只减少",
      tone: "positive",
      note: "相邻官方快照份额差，不是资金金额",
      basis: { kind: "calculated", label: "公式计算", detail: "当日官方份额 − 前一交易日官方份额" },
    },
    {
      id: "southbound",
      eyebrow: "南向净买入",
      value: "+109.97",
      unit: "亿港元",
      delta: "沪港通 +82.05 · 深港通 +27.92",
      tone: "positive",
      note: "官方买入额 − 卖出额",
      basis: { kind: "calculated", label: "公式计算", detail: "港交所官方买入成交额 − 卖出成交额" },
    },
    {
      id: "margin",
      eyebrow: "沪深融资余额",
      value: "2.8693",
      unit: "万亿元",
      delta: "较前日 −109.58 亿元",
      tone: "negative",
      note: "沪深两市合计",
      basis: { kind: "official", label: "官方汇总", detail: "上交所与深交所融资余额合计" },
    },
    {
      id: "northbound",
      eyebrow: "北向成交总额",
      value: "4,159.04",
      unit: "亿元",
      delta: "沪股通 1,823.28 · 深股通 2,335.77",
      tone: "neutral",
      note: "当前官方不披露北向净流入",
      basis: { kind: "official", label: "官方汇总", detail: "港交所北向成交总额，不推断资金方向" },
    },
  ],
  etfFlows: {
    inflow: [
      { code: "588170", name: "科创半导", value: 13.98 },
      { code: "515880", name: "通信ETF", value: 11.2 },
      { code: "515050", name: "5GETF", value: 7.06 },
      { code: "560780", name: "半导设备", value: 6.27 },
      { code: "512100", name: "1000ETF", value: 5.08 },
    ],
    outflow: [
      { code: "512170", name: "医疗ETF", value: -5.36 },
      { code: "513130", name: "恒生科技", value: -5.0 },
      { code: "560860", name: "工业有色", value: -4.36 },
      { code: "512010", name: "医药ETF", value: -3.03 },
      { code: "513770", name: "港股互联", value: -2.02 },
    ],
  },
  marginSeries: [
    { date: "07-10", balance: 29137.69, label: "2.9138 万亿", change: null, financingBuy: 3203.38, lendingBalance: 219.69, lendingSellVolume: 1.32, totalBalance: 29357.38, sseBalance: 14712.72, szseBalance: 14424.97 },
    { date: "07-13", balance: 28802.36, label: "2.8802 万亿", change: -335.33, financingBuy: 2322.52, lendingBalance: 210.61, lendingSellVolume: 1.03, totalBalance: 29012.97, sseBalance: 14575.17, szseBalance: 14227.19 },
    { date: "07-14", balance: 28692.78, label: "2.8693 万亿", change: -109.58, financingBuy: 2408.55, lendingBalance: 213.21, lendingSellVolume: 0.88, totalBalance: 28905.99, sseBalance: 14520.14, szseBalance: 14172.64 },
  ],
  connect: [
    { name: "沪港通南向", value: "+82.05", unit: "亿港元", description: "成交 751.20 亿港元", tone: "positive" },
    { name: "深港通南向", value: "+27.92", unit: "亿港元", description: "成交 538.52 亿港元", tone: "positive" },
    { name: "沪股通北向", value: "1,823.28", unit: "亿元", description: "仅官方成交额", tone: "neutral" },
    { name: "深股通北向", value: "2,335.77", unit: "亿元", description: "仅官方成交额", tone: "neutral" },
  ],
  connectSeries: [
    { date: "07-10", southboundNet: -59.95, southboundTurnover: 1554.56, southboundSseNet: -33.44, southboundSzseNet: -26.51, northboundTurnover: 4492.8, northboundEtfTurnover: 69.76, northboundTradeCount: 16965597 },
    { date: "07-13", southboundNet: 90.38, southboundTurnover: 1272.25, southboundSseNet: 78.74, southboundSzseNet: 11.64, northboundTurnover: 4178.11, northboundEtfTurnover: 89.61, northboundTradeCount: 17355529 },
    { date: "07-14", southboundNet: 109.97, southboundTurnover: 1289.72, southboundSseNet: 82.05, southboundSzseNet: 27.92, northboundTurnover: 4159.04, northboundEtfTurnover: 65.6, northboundTradeCount: 17561612 },
  ],
  dataStatus: [
    { name: "上交所 ETF", status: "ready", asOf: "07-14", detail: "官方日期 · 878 只·3 日", basis: "official" },
    { name: "深交所 ETF", status: "ready", asOf: "07-15", detail: "官方列表页规模日期", basis: "official" },
    { name: "融资融券", status: "ready", asOf: "07-14", detail: "官方沪深汇总", basis: "official" },
    { name: "互联互通", status: "ready", asOf: "07-14", detail: "南向复算 · 北向官方成交", basis: "mixed" },
  ],
  etfTerminal: {
    dailySeries: [{ date: "07-14", value: 79.89, coverage: 878 }],
    availablePeriods: ["1d"],
    classification: "官方产品/投资类型优先，版本化名称规则补充；低置信度产品进入‘未归类’，不强行归入行业",
    classificationVersion: "2026-07-v3",
    universe: { total: 10, excluded: 0, exclusionCounts: {}, marketCounts: { SSE: 10, SZSE: 0 }, categoryCounts: { "科技": 4, "宽基": 1, "医药": 2, "周期资源": 1, "港股海外": 2 }, periodComparable: 10 },
    groupPeriods: [
      { name: "科技", groupType: "industry", periods: { "1d": 38.51 }, componentCount: 4, comparableCount: { "1d": 4 } },
      { name: "宽基", groupType: "broad", periods: { "1d": 5.08 }, componentCount: 1, comparableCount: { "1d": 1 } },
      { name: "医药", groupType: "industry", periods: { "1d": -8.39 }, componentCount: 2, comparableCount: { "1d": 2 } },
      { name: "周期资源", groupType: "industry", periods: { "1d": -4.36 }, componentCount: 1, comparableCount: { "1d": 1 } },
      { name: "港股海外", groupType: "cross_border", periods: { "1d": -7.02 }, componentCount: 2, comparableCount: { "1d": 2 } },
    ],
    instruments: [
      { code: "588170", name: "科创半导", market: "SSE", category: "科技", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "technology", includeInGroup: true, shares: null, periods: { "1d": 13.98 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "515880", name: "通信ETF", market: "SSE", category: "科技", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "technology", includeInGroup: true, shares: null, periods: { "1d": 11.2 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "515050", name: "5GETF", market: "SSE", category: "科技", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "technology", includeInGroup: true, shares: null, periods: { "1d": 7.06 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "560780", name: "半导设备", market: "SSE", category: "科技", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "technology", includeInGroup: true, shares: null, periods: { "1d": 6.27 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "512100", name: "1000ETF", market: "SSE", category: "宽基", groupType: "broad", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "broad_market", includeInGroup: true, shares: null, periods: { "1d": 5.08 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "512170", name: "医疗ETF", market: "SSE", category: "医药", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "healthcare", includeInGroup: true, shares: null, periods: { "1d": -5.36 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "513130", name: "恒生科技", market: "SSE", category: "港股海外", groupType: "cross_border", assetClass: "跨境", classificationSource: "official_etf_type", classificationConfidence: "high", classificationRule: "cross_border", includeInGroup: true, shares: null, periods: { "1d": -5 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "560860", name: "工业有色", market: "SSE", category: "周期资源", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "cyclical_resources", includeInGroup: true, shares: null, periods: { "1d": -4.36 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "512010", name: "医药ETF", market: "SSE", category: "医药", groupType: "industry", assetClass: "股票", classificationSource: "name_rule", classificationConfidence: "medium", classificationRule: "healthcare", includeInGroup: true, shares: null, periods: { "1d": -3.03 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
      { code: "513770", name: "港股互联", market: "SSE", category: "港股海外", groupType: "cross_border", assetClass: "跨境", classificationSource: "official_etf_type", classificationConfidence: "high", classificationRule: "cross_border", includeInGroup: true, shares: null, periods: { "1d": -2.02 }, estimatedCashPeriods: { "1d": null }, coverage: 2 },
    ],
  },
};
