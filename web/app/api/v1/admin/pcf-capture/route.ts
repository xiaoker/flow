import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const ETF_CODE = /^\d{6}$/;
const MAX_BODY_BYTES = 2_000_000;
const MAX_ITEMS = 2_000;
const ITEM_BATCH_SIZE = 25;

type PlanItem = {
  market: string;
  etfCode: string;
  etfName: string;
  currentShares: number | null;
  previousShares: number | null;
  shareDelta: number | null;
  baselineStatus: string;
  required: boolean;
  state: string;
};

type CapturePlan = {
  targetDate: string;
  previousDate: string;
  calculationVersion: "pcf-capture-v2";
  hardLimit: number;
  status: "PLANNED" | "READY_NO_CHANGE" | "BLOCKED_QUALITY" | "BLOCKED_UNIVERSE_ANOMALY";
  planSha256: string;
  items: PlanItem[];
  counts: {
    expected: number;
    required: number;
    unchanged: number;
    newBaselineMissing: number;
    missingCurrent: number;
    invalidShare: number;
  };
};

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function unauthorized(): Response {
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isPlanItem(value: unknown): value is PlanItem {
  if (!isRecord(value)) return false;
  const integerOrNull = (candidate: unknown) => candidate === null || (Number.isSafeInteger(candidate) && Number(candidate) >= 0);
  return Boolean(
    typeof value.market === "string" && /^[A-Z]{2,12}$/.test(value.market) &&
      typeof value.etfCode === "string" && ETF_CODE.test(value.etfCode) &&
      typeof value.etfName === "string" && value.etfName.length > 0 && value.etfName.length <= 200 &&
      integerOrNull(value.currentShares) && integerOrNull(value.previousShares) &&
      (value.shareDelta === null || Number.isSafeInteger(value.shareDelta)) &&
      typeof value.baselineStatus === "string" &&
      typeof value.required === "boolean" && typeof value.state === "string",
  );
}

function isPlan(value: unknown): value is CapturePlan {
  if (!isRecord(value) || !isRecord(value.counts)) return false;
  const counts = value.counts;
  return Boolean(
    value.calculationVersion === "pcf-capture-v2" &&
      typeof value.targetDate === "string" && ISO_DATE.test(value.targetDate) &&
      typeof value.previousDate === "string" && ISO_DATE.test(value.previousDate) &&
      typeof value.hardLimit === "number" && Number.isSafeInteger(value.hardLimit) && value.hardLimit > 0 &&
      ["PLANNED", "READY_NO_CHANGE", "BLOCKED_QUALITY", "BLOCKED_UNIVERSE_ANOMALY"].includes(String(value.status)) &&
      typeof value.planSha256 === "string" && SHA256.test(value.planSha256) &&
      Array.isArray(value.items) && value.items.length <= MAX_ITEMS && value.items.every(isPlanItem) &&
      Object.values(counts).every((candidate) => Number.isSafeInteger(candidate) && Number(candidate) >= 0) &&
      Number(counts.expected) === value.items.length &&
      Number(counts.required) === value.items.filter((item) => item.required).length,
  );
}

function jobIdFor(plan: CapturePlan): string {
  return `pcf:${plan.targetDate}:${plan.planSha256.slice(0, 16)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

function errorResponse(error: unknown): Response {
  console.error("PCF capture control-plane request failed", error);
  return Response.json({ error: "pcf_capture_control_plane_unavailable" }, { status: 503 });
}

async function readJson(request: Request): Promise<unknown> {
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > MAX_BODY_BYTES) throw new Error("payload_too_large");
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) throw new Error("payload_too_large");
  return JSON.parse(body);
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB) return Response.json({ error: "database_unavailable" }, { status: 503 });

  let value: unknown;
  try {
    value = await readJson(request);
  } catch (error) {
    const message = error instanceof Error ? error.message : "invalid_json";
    return Response.json({ error: message === "payload_too_large" ? message : "invalid_json" }, { status: message === "payload_too_large" ? 413 : 400 });
  }
  if (!isPlan(value)) return Response.json({ error: "invalid_pcf_capture_plan" }, { status: 422 });
  const plan = value;
  const jobId = jobIdFor(plan);
  const startedAt = nowIso();
  // Preserve the exact planner status.  In particular, a universe anomaly is
  // not a generic quality failure: downstream reconciliation must be able to
  // distinguish a bounded-source problem from malformed source data.
  const jobStatus = plan.status;

  try {
    const existing = await runtime.DB.prepare(
      `SELECT job_id, status, expected_count, required_count, captured_count, valid_count,
              failed_count, coverage_ppm, updated_at
         FROM pcf_capture_job
        WHERE plan_sha256 = ?
        LIMIT 1`,
    ).bind(plan.planSha256).first<Record<string, unknown>>();
    if (existing) {
      // A previous run can die between the job row and one of its item
      // batches.  Reconcile the plan rows with INSERT OR IGNORE so a retry
      // repairs that gap without touching captured checkpoints.
      await ensurePlanItems(runtime.DB, String(existing.job_id), plan.items, startedAt);
      return Response.json({ jobId: existing.job_id, idempotent: true, job: existing });
    }

    await runtime.DB.prepare(
      `INSERT INTO pcf_capture_job (
         job_id, target_date, previous_date, plan_sha256, calculation_version, status,
         expected_count, required_count, unchanged_count, new_baseline_count,
         missing_current_count, captured_count, valid_count, failed_count, coverage_ppm,
         error_json, started_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)`,
    ).bind(
      jobId,
      plan.targetDate,
      plan.previousDate,
      plan.planSha256,
      plan.calculationVersion,
      jobStatus,
      plan.counts.expected,
      plan.counts.required,
      plan.counts.unchanged,
      plan.counts.newBaselineMissing,
      plan.counts.missingCurrent,
      JSON.stringify(plan.status === "BLOCKED_UNIVERSE_ANOMALY" ? { reason: plan.status } : {}),
      startedAt,
      startedAt,
    ).run();

    await ensurePlanItems(runtime.DB, jobId, plan.items, startedAt);
    return Response.json({ jobId, idempotent: false, status: jobStatus, targetDate: plan.targetDate });
  } catch (error) {
    return errorResponse(error);
  }
}

async function ensurePlanItems(db: D1Database, jobId: string, items: PlanItem[], updatedAt: string): Promise<void> {
  for (let offset = 0; offset < items.length; offset += ITEM_BATCH_SIZE) {
    const statements = items.slice(offset, offset + ITEM_BATCH_SIZE).map((item) => db.prepare(
      `INSERT OR IGNORE INTO pcf_capture_item (
         job_id, etf_code, etf_name, current_shares, previous_shares, share_delta,
         baseline_status, state, attempts, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)`,
    ).bind(
      jobId,
      item.etfCode,
      item.etfName,
      item.currentShares,
      item.previousShares,
      item.shareDelta,
      item.baselineStatus,
      item.required ? "PENDING" : "NOT_REQUIRED",
      updatedAt,
    ));
    if (statements.length) await db.batch(statements);
  }
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB) return Response.json({ error: "database_unavailable" }, { status: 503 });
  const search = new URL(request.url).searchParams;
  const jobId = search.get("jobId");
  const targetDate = search.get("date");
  if (!jobId && (!targetDate || !ISO_DATE.test(targetDate))) return Response.json({ error: "jobId_or_date_required" }, { status: 400 });
  try {
    const job = jobId
      ? await runtime.DB.prepare("SELECT * FROM pcf_capture_job WHERE job_id = ? LIMIT 1").bind(jobId).first<Record<string, unknown>>()
      : await runtime.DB.prepare("SELECT * FROM pcf_capture_job WHERE target_date = ? ORDER BY updated_at DESC LIMIT 1").bind(targetDate).first<Record<string, unknown>>();
    if (!job) return Response.json({ error: "pcf_capture_job_not_found" }, { status: 404 });
    const counts = await runtime.DB.prepare(
      `SELECT baseline_status, state, COUNT(*) AS count
         FROM pcf_capture_item WHERE job_id = ? GROUP BY baseline_status, state`,
    ).bind(job.job_id).all<Record<string, unknown>>();
    const items = await runtime.DB.prepare(
      `SELECT etf_code AS etfCode, state
         FROM pcf_capture_item WHERE job_id = ? ORDER BY etf_code`,
    ).bind(job.job_id).all<Record<string, unknown>>();
    return Response.json(
      { job, itemCounts: counts.results ?? [], items: items.results ?? [] },
      { headers: { "Cache-Control": "private, no-store" } },
    );
  } catch (error) {
    return errorResponse(error);
  }
}
