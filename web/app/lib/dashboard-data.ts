export type FlowDirection = "inflow" | "outflow";

export type FlowTone = "positive" | "negative" | "neutral";

export type FlowPeriod = "1d" | "3d" | "1w" | "2w" | "1m" | "3m" | "6m" | "ytd" | "12m";

export interface EtfTerminalInstrument {
  code: string;
  name: string;
  category: string;
  shares: number | null;
  periods: Partial<Record<FlowPeriod, number | null>>;
  coverage: number;
}

export interface EtfTerminalData {
  dailySeries: Array<{ date: string; value: number; coverage: number }>;
  instruments: EtfTerminalInstrument[];
  groupPeriods: Array<{
    name: string;
    periods: Partial<Record<FlowPeriod, number | null>>;
  }>;
  availablePeriods?: FlowPeriod[];
  classification?: string;
}

export interface DashboardSnapshot {
  asOf: string;
  observedAt: string;
  mode: string;
  gate: string;
  quality: { pass: number; warn: number; fail: number };
  metrics: Array<{
    id: string;
    eyebrow: string;
    value: string;
    unit: string;
    delta: string;
    tone: FlowTone;
    note: string;
  }>;
  etfFlows: Record<FlowDirection, Array<{ code: string; name: string; value: number }>>;
  marginSeries: Array<{ date: string; balance: number; label: string }>;
  connect: Array<{
    name: string;
    value: string;
    unit: string;
    description: string;
    tone: FlowTone;
  }>;
  dataStatus: Array<{
    name: string;
    status: "ready" | "warning";
    asOf: string;
    detail: string;
  }>;
  etfTerminal?: EtfTerminalData;
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
      eyebrow: "上交所 ETF 份额净变化",
      value: "+79.89",
      unit: "亿份",
      delta: "299 只增加 · 229 只减少",
      tone: "positive",
      note: "份额口径，非净申购金额",
    },
    {
      id: "southbound",
      eyebrow: "南向净买入",
      value: "+109.97",
      unit: "亿港元",
      delta: "沪港通 +82.05 · 深港通 +27.92",
      tone: "positive",
      note: "官方买入额 − 卖出额",
    },
    {
      id: "margin",
      eyebrow: "沪深融资余额",
      value: "2.8693",
      unit: "万亿元",
      delta: "较前日 −109.58 亿元",
      tone: "negative",
      note: "沪深两市合计",
    },
    {
      id: "northbound",
      eyebrow: "北向成交总额",
      value: "4,159.04",
      unit: "亿元",
      delta: "沪股通 1,823.28 · 深股通 2,335.77",
      tone: "neutral",
      note: "当前官方不披露北向净流入",
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
    { date: "07-10", balance: 29137.69, label: "2.9138 万亿" },
    { date: "07-13", balance: 28802.36, label: "2.8802 万亿" },
    { date: "07-14", balance: 28692.78, label: "2.8693 万亿" },
  ],
  connect: [
    { name: "沪港通南向", value: "+82.05", unit: "亿港元", description: "成交 751.20 亿港元", tone: "positive" },
    { name: "深港通南向", value: "+27.92", unit: "亿港元", description: "成交 538.52 亿港元", tone: "positive" },
    { name: "沪股通北向", value: "1,823.28", unit: "亿元", description: "仅官方成交额", tone: "neutral" },
    { name: "深股通北向", value: "2,335.77", unit: "亿元", description: "仅官方成交额", tone: "neutral" },
  ],
  dataStatus: [
    { name: "上交所 ETF", status: "ready", asOf: "07-14", detail: "878 只·3 日" },
    { name: "深交所 ETF", status: "warning", asOf: "07-15", detail: "当前快照" },
    { name: "融资融券", status: "ready", asOf: "07-14", detail: "沪深完整" },
    { name: "互联互通", status: "ready", asOf: "07-14", detail: "4 通道" },
  ],
  etfTerminal: {
    dailySeries: [{ date: "07-14", value: 79.89, coverage: 878 }],
    availablePeriods: ["1d"],
    classification: "基金简称关键词映射",
    groupPeriods: [
      { name: "科技", periods: { "1d": 38.51 } },
      { name: "宽基", periods: { "1d": 5.08 } },
      { name: "医药", periods: { "1d": -8.39 } },
      { name: "商品", periods: { "1d": -4.36 } },
      { name: "港股海外", periods: { "1d": -7.02 } },
    ],
    instruments: [
      { code: "588170", name: "科创半导", category: "科技", shares: null, periods: { "1d": 13.98 }, coverage: 2 },
      { code: "515880", name: "通信ETF", category: "科技", shares: null, periods: { "1d": 11.2 }, coverage: 2 },
      { code: "515050", name: "5GETF", category: "科技", shares: null, periods: { "1d": 7.06 }, coverage: 2 },
      { code: "560780", name: "半导设备", category: "科技", shares: null, periods: { "1d": 6.27 }, coverage: 2 },
      { code: "512100", name: "1000ETF", category: "宽基", shares: null, periods: { "1d": 5.08 }, coverage: 2 },
      { code: "512170", name: "医疗ETF", category: "医药", shares: null, periods: { "1d": -5.36 }, coverage: 2 },
      { code: "513130", name: "恒生科技", category: "港股海外", shares: null, periods: { "1d": -5 }, coverage: 2 },
      { code: "560860", name: "工业有色", category: "商品", shares: null, periods: { "1d": -4.36 }, coverage: 2 },
      { code: "512010", name: "医药ETF", category: "医药", shares: null, periods: { "1d": -3.03 }, coverage: 2 },
      { code: "513770", name: "港股互联", category: "港股海外", shares: null, periods: { "1d": -2.02 }, coverage: 2 },
    ],
  },
};
