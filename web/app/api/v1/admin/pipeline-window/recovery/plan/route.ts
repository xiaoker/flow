import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  getPipelineWindowRecovery,
  PipelineWindowError,
} from "@/app/lib/pipeline-window-recovery";
import { parsePipelineRunKey } from "@/app/lib/pipeline-window";

export const dynamic = "force-dynamic";

const SHA256 = /^[0-9a-f]{64}$/;
const PLAN_KEY = /^market-data\/recovery\/plans\/[A-Za-z0-9._:-]{1,120}\.json$/;
const MAX_PLAN_BYTES = 300_000;
const PLAN_SCHEMA_VERSION = 1;
const PLAN_CONTRACT_VERSION = "recovery-execution-v1";
const RECOVERY_STAGES = new Set([
  "core_collection",
  "dashboard",
  "etf_penetration",
  "stock_flow",
  "pcf_exact",
  "publish",
]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z$/;
const IDENTIFIER = /^[A-Za-z0-9:_./-]{1,220}$/;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function response(body: Record<string, unknown>, status = 200): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "private, no-store" } });
}

function enabled(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

function authorized(request: Request, expected: string): boolean {
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  return Boolean(expected && supplied && secureEqual(supplied, expected));
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function validDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function isolatedPath(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0 || value.includes("..") || value.includes("\\") || value.startsWith("/")) return false;
  return value === "recovery" || value === ".recovery" || value.startsWith("recovery/") || value.startsWith(".recovery/");
}

function validPlanScope(plan: Record<string, unknown>): boolean {
  if (plan.schemaVersion !== PLAN_SCHEMA_VERSION || plan.contractVersion !== PLAN_CONTRACT_VERSION) return false;
  if (typeof plan.campaignId !== "string" || !IDENTIFIER.test(plan.campaignId) || typeof plan.recoveryId !== "string" || !IDENTIFIER.test(plan.recoveryId)) return false;
  if (!parsePipelineRunKey(String(plan.runKey ?? ""))) return false;
  if (!Array.isArray(plan.dataDates) || plan.dataDates.length < 1 || plan.dataDates.length > 3) return false;
  const dates = plan.dataDates;
  if (dates.some((value) => !validDate(value)) || new Set(dates).size !== dates.length || dates.some((value, index) => index > 0 && String(dates[index - 1]) >= String(value))) return false;
  if (!Array.isArray(plan.requiredStages) || plan.requiredStages.length < 1 || plan.requiredStages.some((value) => typeof value !== "string" || !RECOVERY_STAGES.has(value)) || new Set(plan.requiredStages).size !== plan.requiredStages.length) return false;
  if (!Number.isSafeInteger(plan.maxDates) || Number(plan.maxDates) < dates.length || Number(plan.maxDates) > 3) return false;
  if (!Number.isSafeInteger(plan.maxRequests) || Number(plan.maxRequests) < 1 || Number(plan.maxRequests) > 10_000) return false;
  if (typeof plan.deadline !== "string" || !ISO_INSTANT.test(plan.deadline) || Number.isNaN(Date.parse(plan.deadline))) return false;
  if (typeof plan.budgetManifest === "string") {
    if (!IDENTIFIER.test(plan.budgetManifest) || !(plan.budgetManifest.startsWith("ops-budget/") || plan.budgetManifest.startsWith("recovery/"))) return false;
  } else if (!plan.budgetManifest || typeof plan.budgetManifest !== "object" || Array.isArray(plan.budgetManifest)) {
    return false;
  }
  return isolatedPath(plan.artifactRoot) && isolatedPath(plan.stateDir);
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime.PIPELINE_RECOVERY_ENABLED)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB || !runtime.RAW_BUCKET) return response({ error: "storage_unavailable" }, 503);
  const recoveryId = new URL(request.url).searchParams.get("recoveryId") ?? "";
  if (!recoveryId) return response({ error: "invalid_recovery_id" }, 422);
  try {
    const recovery = await getPipelineWindowRecovery(runtime.DB, recoveryId);
    if (!recovery) return response({ error: "recovery_not_found" }, 404);
    const key = recovery.plan_key;
    if (!PLAN_KEY.test(key) || key.includes("..") || key.includes("//")) return response({ error: "invalid_plan_key" }, 422);
    if (!SHA256.test(recovery.plan_sha256)) return response({ error: "invalid_plan_sha256" }, 422);
    if (!SHA256.test(recovery.scope_sha256)) return response({ error: "invalid_scope_sha256" }, 422);
    const head = await runtime.RAW_BUCKET.head(key);
    // plan_sha256 is the semantic hash inside the plan; scope_sha256 is the
    // exact immutable R2 object hash.  They are intentionally separate so a
    // pretty-printed object cannot be mistaken for a different plan.
    if (!head || head.size === undefined || head.size <= 0 || head.size > MAX_PLAN_BYTES || head.customMetadata?.sha256 !== recovery.scope_sha256) {
      return response({ error: "plan_object_not_verified" }, 409);
    }
    const object = await runtime.RAW_BUCKET.get(key);
    if (!object) return response({ error: "plan_object_not_found" }, 409);
    const bytes = await object.arrayBuffer();
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_PLAN_BYTES) return response({ error: "plan_object_size_invalid" }, 409);
    if (!secureEqual(await sha256Hex(bytes), recovery.scope_sha256)) return response({ error: "plan_object_sha256_mismatch" }, 409);
    let plan: unknown;
    try {
      plan = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return response({ error: "plan_json_invalid" }, 409);
    }
    if (!isRecord(plan) || typeof plan.planHash !== "string" || !SHA256.test(plan.planHash) || !secureEqual(plan.planHash, recovery.plan_sha256) || plan.recoveryId !== recovery.recovery_id || plan.runKey !== recovery.run_key) {
      return response({ error: "plan_binding_mismatch" }, 409);
    }
    const withoutHash = { ...plan };
    delete withoutHash.planHash;
    const semanticHash = await sha256Hex(new TextEncoder().encode(canonicalJson(withoutHash)).buffer);
    if (!secureEqual(semanticHash, recovery.plan_sha256)) return response({ error: "plan_semantic_sha256_mismatch" }, 409);
    if (!validPlanScope(plan)) {
      return response({ error: "plan_scope_invalid" }, 409);
    }
    return response({ recoveryId, planKey: key, planSha256: recovery.plan_sha256, scopeSha256: recovery.scope_sha256, plan });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery plan read failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_plan_failed" }, 503);
  }
}
