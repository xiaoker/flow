import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const SOURCE_ID = /^[a-z0-9_]{3,64}$/;
const STATUS = new Set(["ready", "pending", "error"]);
const MAX_BODY_BYTES = 600_000;
const SOURCE_LABELS: Record<string, string> = {
  sse_etf: "上交所 ETF",
  szse_etf: "深交所 ETF",
  sse_margin: "上交所融资融券",
  szse_margin: "深交所融资融券",
  bse_margin: "北交所融资融券",
  stock_connect_summary: "港交所互联互通",
  sse_etf_nav: "上交所 ETF 单位净值",
  sse_etf_pcf: "上交所 ETF PCF",
};

type ReadinessSource = {
  sourceId: string;
  label: string;
  required: boolean;
  status: "ready" | "pending" | "error";
  attempt: number;
  observedAt: string;
  nextCheckAt: string | null;
  deadlineAt: string;
  deadlineExceeded: boolean;
  observedDates: string[];
  watermark: Record<string, unknown>;
  reason: string;
  runId: string;
  evidence: Record<string, unknown>;
};

type ReadinessLedger = {
  schemaVersion: 1;
  targetDate: string;
  requestedDates: string[];
  runId: string;
  generatedAt: string;
  observedAt: string;
  deadlineAt: string;
  state: "ready" | "pending" | "error";
  pendingDomains: string[];
  errorDomains: string[];
  nextCheckAt: string | null;
  sources: ReadinessSource[];
};

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isDate(value: unknown): value is string {
  return typeof value === "string" && ISO_DATE.test(value);
}

function isInstant(value: unknown): value is string {
  return typeof value === "string" && ISO_INSTANT.test(value) && !Number.isNaN(Date.parse(value));
}

function isSource(value: unknown): value is ReadinessSource {
  if (!isRecord(value)) return false;
  return Boolean(
    typeof value.sourceId === "string" && SOURCE_ID.test(value.sourceId) &&
      typeof value.label === "string" && value.label.length > 0 &&
      typeof value.required === "boolean" && STATUS.has(String(value.status)) &&
      Number.isSafeInteger(value.attempt) && Number(value.attempt) >= 1 &&
      isInstant(value.observedAt) &&
      (value.nextCheckAt === null || isInstant(value.nextCheckAt)) &&
      isInstant(value.deadlineAt) && typeof value.deadlineExceeded === "boolean" &&
      Array.isArray(value.observedDates) && value.observedDates.every(isDate) &&
      isRecord(value.watermark) && typeof value.reason === "string" && value.reason.length > 0 &&
      typeof value.runId === "string" && value.runId.length > 0 && isRecord(value.evidence),
  );
}

function isLedger(value: unknown): value is ReadinessLedger {
  if (!isRecord(value)) return false;
  return Boolean(
    value.schemaVersion === 1 && isDate(value.targetDate) &&
      Array.isArray(value.requestedDates) && value.requestedDates.every(isDate) &&
      typeof value.runId === "string" && value.runId.length > 0 &&
      isInstant(value.generatedAt) && isInstant(value.observedAt) && isInstant(value.deadlineAt) &&
      STATUS.has(String(value.state)) && Array.isArray(value.pendingDomains) &&
      value.pendingDomains.every((item) => typeof item === "string") &&
      Array.isArray(value.errorDomains) && value.errorDomains.every((item) => typeof item === "string") &&
      (value.nextCheckAt === null || isInstant(value.nextCheckAt)) &&
      Array.isArray(value.sources) && value.sources.length > 0 && value.sources.every(isSource),
  );
}

function unauthorized() {
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

function stateFor(rows: Array<{ status: string; required: number }>): "ready" | "pending" | "error" {
  const required = rows.filter((row) => Number(row.required) === 1);
  if (required.some((row) => row.status === "error")) return "error";
  if (required.some((row) => row.status === "pending")) return "pending";
  return "ready";
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB) return Response.json({ error: "database_unavailable" }, { status: 503 });

  const targetDate = new URL(request.url).searchParams.get("date");
  if (!targetDate || !ISO_DATE.test(targetDate)) return Response.json({ error: "invalid_date" }, { status: 400 });
  try {
    const result = await runtime.DB.prepare(
      `SELECT target_date, source_id, status, overall_status, required, observed_at,
              next_check_at, deadline_at, deadline_exceeded, attempt,
              observed_dates_json, watermark_json, reason, run_id, evidence_json, updated_at
         FROM source_readiness
        WHERE target_date = ?
        ORDER BY required DESC, source_id ASC`,
    ).bind(targetDate).all<Record<string, unknown>>();
    const rows = result.results ?? [];
    if (!rows.length) return Response.json({ error: "source_readiness_not_found", targetDate }, { status: 404 });
    const sources = rows.map((row) => ({
      sourceId: row.source_id,
      label: SOURCE_LABELS[String(row.source_id)] ?? String(row.source_id),
      status: row.status,
      overallStatus: row.overall_status,
      required: Number(row.required) === 1,
      observedAt: row.observed_at,
      nextCheckAt: row.next_check_at,
      deadlineAt: row.deadline_at,
      deadlineExceeded: Number(row.deadline_exceeded) === 1,
      attempt: Number(row.attempt),
      observedDates: JSON.parse(String(row.observed_dates_json || "[]")),
      watermark: JSON.parse(String(row.watermark_json || "{}")),
      reason: row.reason,
      runId: row.run_id,
      evidence: JSON.parse(String(row.evidence_json || "{}")),
      updatedAt: row.updated_at,
    }));
    const state = stateFor(rows.map((row) => ({ status: String(row.status), required: Number(row.required) })));
    return Response.json(
      {
        schemaVersion: 1,
        targetDate,
        state,
        sources,
        pendingDomains: sources.filter((row) => row.required && row.status === "pending").map((row) => row.label),
        errorDomains: sources.filter((row) => row.required && row.status === "error").map((row) => row.label),
        nextCheckAt: sources
          .map((row) => row.nextCheckAt)
          .filter((value): value is string => typeof value === "string")
          .sort()[0] ?? null,
      },
      { headers: { "Cache-Control": "private, no-store" } },
    );
  } catch (error) {
    console.error("source readiness read failed", error);
    return Response.json({ error: "source_readiness_unavailable" }, { status: 503 });
  }
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB) return Response.json({ error: "database_unavailable" }, { status: 503 });
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) return Response.json({ error: "payload_too_large" }, { status: 413 });
  let value: unknown;
  try {
    const body = await request.text();
    if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return Response.json({ error: "payload_too_large" }, { status: 413 });
    value = JSON.parse(body);
  } catch {
    return Response.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!isLedger(value)) return Response.json({ error: "invalid_source_readiness" }, { status: 422 });
  try {
    const statements: D1PreparedStatement[] = [];
    for (const source of value.sources) {
      const existing = await runtime.DB.prepare(
        `SELECT observed_at, run_id, attempt FROM source_readiness WHERE target_date = ? AND source_id = ?`,
      ).bind(value.targetDate, source.sourceId).first<{ observed_at: string; run_id: string; attempt: number }>();
      // A retry of the same run is idempotent.  A late observation may never
      // overwrite a newer watermark, otherwise an old 08:45 run could erase
      // the 09:15 ready state.
      if (existing?.run_id === value.runId) continue;
      if (existing?.observed_at && Date.parse(existing.observed_at) > Date.parse(source.observedAt)) continue;
      const attempt = Math.max(Number(source.attempt) || 1, Number(existing?.attempt || 0) + 1);
      statements.push(
        runtime.DB.prepare(
          `INSERT INTO source_readiness (
             target_date, source_id, status, overall_status, required,
             observed_at, next_check_at, deadline_at, deadline_exceeded, attempt,
             observed_dates_json, watermark_json, reason, run_id, evidence_json, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(target_date, source_id) DO UPDATE SET
             status = excluded.status,
             overall_status = excluded.overall_status,
             required = excluded.required,
             observed_at = excluded.observed_at,
             next_check_at = excluded.next_check_at,
             deadline_at = excluded.deadline_at,
             deadline_exceeded = excluded.deadline_exceeded,
             attempt = excluded.attempt,
             observed_dates_json = excluded.observed_dates_json,
             watermark_json = excluded.watermark_json,
             reason = excluded.reason,
             run_id = excluded.run_id,
             evidence_json = excluded.evidence_json,
             updated_at = excluded.updated_at`,
        ).bind(
          value.targetDate,
          source.sourceId,
          source.status,
          value.state,
          source.required ? 1 : 0,
          source.observedAt,
          source.nextCheckAt,
          source.deadlineAt,
          source.deadlineExceeded ? 1 : 0,
          attempt,
          JSON.stringify(source.observedDates),
          JSON.stringify(source.watermark),
          source.reason,
          value.runId,
          JSON.stringify(source.evidence),
          value.observedAt,
        ),
      );
    }
    if (statements.length) {
      if (typeof runtime.DB.batch === "function") await runtime.DB.batch(statements);
      else for (const statement of statements) await statement.run();
    }
    return Response.json({ status: statements.length ? "recorded" : "already_recorded", targetDate: value.targetDate, state: value.state, nextCheckAt: value.nextCheckAt }, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    console.error("source readiness write failed", error);
    return Response.json({ error: "source_readiness_write_failed" }, { status: 500 });
  }
}
