import { dashboardSnapshot, type DashboardSnapshot } from "./dashboard-data";
import {
  enrichSnapshotWithEtfFlowState,
  loadEtfFlowState,
} from "./etf-flow-state";
import {
  enrichSnapshotWithMarketHistory,
  MARKET_HISTORY_MAX_DATES,
  type MarketHistoryRow,
} from "./market-history";
import { isPublishableSnapshot } from "./snapshot-policy.mjs";

type RuntimeEnv = {
  DB?: D1Database;
  RAW_BUCKET?: R2Bucket;
  INGEST_API_KEY?: string;
};

const DASHBOARD_CACHE_TTL_MS = 30_000;
const HISTORY_DATES_CACHE_TTL_MS = 60_000;

type DashboardSnapshotCache = {
  db: D1Database;
  bucket?: R2Bucket;
  value: DashboardSnapshot;
  expiresAt: number;
};

let dashboardSnapshotCache: DashboardSnapshotCache | null = null;
let dashboardSnapshotInFlight:
  | { db: D1Database; bucket?: R2Bucket; promise: Promise<DashboardSnapshot> }
  | null = null;

type HistoryDatesCache = {
  db: D1Database;
  value: string[];
  expiresAt: number;
};

let historyDatesCache: HistoryDatesCache | null = null;
let historyDatesInFlight: { db: D1Database; promise: Promise<string[]> } | null = null;

export async function runtimeEnv(): Promise<RuntimeEnv> {
  const injected = (
    globalThis as typeof globalThis & { __FLOW_RUNTIME_ENV__?: RuntimeEnv }
  ).__FLOW_RUNTIME_ENV__;
  if (injected) return injected;
  try {
    const workers = await import("cloudflare:workers");
    return workers.env as unknown as RuntimeEnv;
  } catch {
    return {};
  }
}

export function isDashboardSnapshot(value: unknown): value is DashboardSnapshot {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<DashboardSnapshot>;
  return Boolean(
    typeof candidate.asOf === "string" &&
      /^\d{4}-\d{2}-\d{2}$/.test(candidate.asOf) &&
      typeof candidate.observedAt === "string" &&
      typeof candidate.mode === "string" &&
      typeof candidate.gate === "string" &&
      candidate.quality &&
      Number.isInteger(candidate.quality.pass) &&
      Number.isInteger(candidate.quality.warn) &&
      Number.isInteger(candidate.quality.fail) &&
      Array.isArray(candidate.metrics) &&
      candidate.metrics.length === 4 &&
      candidate.etfFlows &&
      Array.isArray(candidate.etfFlows.inflow) &&
      Array.isArray(candidate.etfFlows.outflow) &&
      Array.isArray(candidate.marginSeries) &&
      Array.isArray(candidate.connect) &&
      Array.isArray(candidate.dataStatus) &&
      (!candidate.etfTerminal ||
        (Array.isArray(candidate.etfTerminal.dailySeries) &&
          Array.isArray(candidate.etfTerminal.instruments) &&
          Array.isArray(candidate.etfTerminal.groupPeriods))),
  );
}

async function loadMarketHistoryRows(
  db: D1Database | undefined,
  asOf: string,
): Promise<MarketHistoryRow[]> {
  if (!db || !/^\d{4}-\d{2}-\d{2}$/.test(asOf)) return [];
  try {
    const result = await db
      .prepare(
        `SELECT
           trade_date, margin_balance, margin_change, financing_buy,
           lending_balance, lending_sell_volume, total_balance, sse_balance,
           szse_balance, southbound_net, southbound_turnover,
           southbound_sse_net, southbound_szse_net, northbound_turnover,
           northbound_etf_turnover, northbound_trade_count, margin_available,
           connect_available, source_snapshot_sha256, observed_at
         FROM market_history_daily
         WHERE trade_date <= ?
         ORDER BY trade_date DESC
         LIMIT ?`,
      )
      .bind(asOf, MARKET_HISTORY_MAX_DATES)
      .all<MarketHistoryRow>();
    return [...(result.results ?? [])].reverse();
  } catch {
    return [];
  }
}

export async function getDashboardSnapshot(): Promise<DashboardSnapshot> {
  const runtime = await runtimeEnv();
  const db = runtime.DB;
  if (!db) return dashboardSnapshot;

  const now = Date.now();
  if (
    dashboardSnapshotCache &&
    dashboardSnapshotCache.db === db &&
    dashboardSnapshotCache.bucket === runtime.RAW_BUCKET &&
    dashboardSnapshotCache.expiresAt > now
  ) {
    return dashboardSnapshotCache.value;
  }
  if (
    dashboardSnapshotInFlight?.db === db &&
    dashboardSnapshotInFlight.bucket === runtime.RAW_BUCKET
  ) {
    return dashboardSnapshotInFlight.promise;
  }

  const promise = readDashboardSnapshot(runtime, db);
  dashboardSnapshotInFlight = { db, bucket: runtime.RAW_BUCKET, promise };
  try {
    const value = await promise;
    dashboardSnapshotCache = {
      db,
      bucket: runtime.RAW_BUCKET,
      value,
      expiresAt: Date.now() + DASHBOARD_CACHE_TTL_MS,
    };
    return value;
  } finally {
    if (dashboardSnapshotInFlight?.promise === promise) dashboardSnapshotInFlight = null;
  }
}

async function readDashboardSnapshot(
  runtime: RuntimeEnv,
  db: D1Database,
): Promise<DashboardSnapshot> {

  try {
    const row = await db
      .prepare(
        `SELECT payload_json
         FROM dashboard_snapshot
         WHERE quality_fail = 0
         ORDER BY published_at DESC
         LIMIT 1`,
      )
      .first<{ payload_json: string }>();
    if (!row) return dashboardSnapshot;
    const parsed: unknown = JSON.parse(row.payload_json);
    if (!isDashboardSnapshot(parsed) || !isPublishableSnapshot(parsed)) {
      return dashboardSnapshot;
    }
    const [state, marketHistory] = await Promise.all([
      loadEtfFlowState(runtime.RAW_BUCKET),
      loadMarketHistoryRows(db, parsed.asOf),
    ]);
    return enrichSnapshotWithMarketHistory(
      enrichSnapshotWithEtfFlowState(parsed, state),
      marketHistory,
    );
  } catch {
    return dashboardSnapshot;
  }
}

type HistoricalSnapshotRow = {
  as_of: string;
  r2_object_key: string;
};

export type DashboardViewData = {
  snapshot: DashboardSnapshot;
  availableDates: string[];
  requestedDate: string | null;
  exactDateMatch: boolean;
};

async function readHistoricalSnapshot(
  requestedDate: string,
): Promise<{ snapshot: DashboardSnapshot; exact: boolean } | null> {
  const runtime = await runtimeEnv();
  if (!runtime.DB || !runtime.RAW_BUCKET || !/^\d{4}-\d{2}-\d{2}$/.test(requestedDate)) {
    return null;
  }
  try {
    let row = await runtime.DB
      .prepare(
        `SELECT as_of, r2_object_key
         FROM historical_snapshot
         WHERE as_of = ?
         LIMIT 1`,
      )
      .bind(requestedDate)
      .first<HistoricalSnapshotRow>();
    let exact = true;
    if (!row) {
      exact = false;
      row = await runtime.DB
        .prepare(
          `SELECT as_of, r2_object_key
           FROM historical_snapshot
           WHERE as_of < ?
           ORDER BY as_of DESC
           LIMIT 1`,
        )
        .bind(requestedDate)
        .first<HistoricalSnapshotRow>();
    }
    if (!row) return null;
    const object = await runtime.RAW_BUCKET.get(row.r2_object_key);
    if (!object) return null;
    const parsed: unknown = JSON.parse(await object.text());
    if (!isDashboardSnapshot(parsed) || !isPublishableSnapshot(parsed)) return null;
    const [state, marketHistory] = await Promise.all([
      loadEtfFlowState(runtime.RAW_BUCKET),
      loadMarketHistoryRows(runtime.DB, parsed.asOf),
    ]);
    return {
      snapshot: enrichSnapshotWithMarketHistory(
        enrichSnapshotWithEtfFlowState(parsed, state),
        marketHistory,
      ),
      exact,
    };
  } catch {
    return null;
  }
}

export async function getAvailableHistoryDates(): Promise<string[]> {
  const db = (await runtimeEnv()).DB;
  if (!db) return [];
  const now = Date.now();
  if (historyDatesCache?.db === db && historyDatesCache.expiresAt > now) {
    return [...historyDatesCache.value];
  }
  if (historyDatesInFlight?.db === db) {
    return historyDatesInFlight.promise.then((value) => [...value]);
  }

  const promise = readAvailableHistoryDates(db);
  historyDatesInFlight = { db, promise };
  try {
    const value = await promise;
    historyDatesCache = {
      db,
      value,
      expiresAt: Date.now() + HISTORY_DATES_CACHE_TTL_MS,
    };
    return [...value];
  } finally {
    if (historyDatesInFlight?.promise === promise) historyDatesInFlight = null;
  }
}

async function readAvailableHistoryDates(db: D1Database): Promise<string[]> {
  try {
    const result = await db
      .prepare(
        `SELECT as_of
         FROM historical_snapshot
         WHERE quality_fail = 0
         ORDER BY as_of ASC`,
      )
      .all<{ as_of: string }>();
    return (result.results ?? []).map((row) => row.as_of);
  } catch {
    return [];
  }
}

export async function getDashboardViewData(
  requestedDate?: string,
): Promise<DashboardViewData> {
  const latest = await getDashboardSnapshot();
  const availableDates = await getAvailableHistoryDates();
  if (!availableDates.includes(latest.asOf)) availableDates.push(latest.asOf);
  availableDates.sort();
  const requested = requestedDate && /^\d{4}-\d{2}-\d{2}$/.test(requestedDate)
    ? requestedDate
    : null;
  if (!requested || requested === latest.asOf) {
    return {
      snapshot: latest,
      availableDates,
      requestedDate: requested,
      exactDateMatch: true,
    };
  }
  const historical = await readHistoricalSnapshot(requested);
  return {
    snapshot: historical?.snapshot ?? latest,
    availableDates,
    requestedDate: requested,
    exactDateMatch: historical?.exact ?? false,
  };
}
