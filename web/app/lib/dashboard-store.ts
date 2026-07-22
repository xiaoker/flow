import { dashboardSnapshot, type DashboardSnapshot } from "./dashboard-data";
import { isPublishableSnapshot } from "./snapshot-policy.mjs";

type RuntimeEnv = {
  DB?: D1Database;
  RAW_BUCKET?: R2Bucket;
  INGEST_API_KEY?: string;
};

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

export async function getDashboardSnapshot(): Promise<DashboardSnapshot> {
  const db = (await runtimeEnv()).DB;
  if (!db) return dashboardSnapshot;

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
    return isDashboardSnapshot(parsed) && isPublishableSnapshot(parsed)
      ? parsed
      : dashboardSnapshot;
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
    return { snapshot: parsed, exact };
  } catch {
    return null;
  }
}

export async function getAvailableHistoryDates(): Promise<string[]> {
  const db = (await runtimeEnv()).DB;
  if (!db) return [];
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
