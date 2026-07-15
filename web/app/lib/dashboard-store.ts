import { dashboardSnapshot, type DashboardSnapshot } from "./dashboard-data";

type RuntimeEnv = {
  DB?: D1Database;
  RAW_BUCKET?: R2Bucket;
  INGEST_API_KEY?: string;
};

export async function runtimeEnv(): Promise<RuntimeEnv> {
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
      Array.isArray(candidate.dataStatus),
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
         ORDER BY published_at DESC
         LIMIT 1`,
      )
      .first<{ payload_json: string }>();
    if (!row) return dashboardSnapshot;
    const parsed: unknown = JSON.parse(row.payload_json);
    return isDashboardSnapshot(parsed) ? parsed : dashboardSnapshot;
  } catch {
    return dashboardSnapshot;
  }
}
