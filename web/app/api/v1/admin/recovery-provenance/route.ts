import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  applyRecoveryProvenancePlan,
  buildRecoveryProvenancePlan,
  parseProvenanceQuery,
  RECOVERY_PROVENANCE_CONTRACT,
  type ProvenanceQuery,
} from "@/app/lib/recovery-provenance";

export const dynamic = "force-dynamic";

const APPLY_INTENT = "recovery-provenance-apply-v1";
const MAX_BODY_BYTES = 16_384;
const SHA256 = /^[a-f0-9]{64}$/;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function response(body: Record<string, unknown>, status = 200): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "private, no-store" } });
}

function authorized(request: Request, expected: string): boolean {
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  return Boolean(expected && supplied && secureEqual(supplied, expected));
}

function queryFromBody(value: unknown): { query: ProvenanceQuery; planHash: string } {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_repair_request");
  const candidate = value as Record<string, unknown>;
  if (candidate.mode !== "apply") throw new Error("invalid_mode");
  const params = new URLSearchParams();
  for (const key of ["start", "end", "cursor", "limit"]) {
    const item = candidate[key];
    if (item !== undefined && item !== null) params.set(key, String(item));
  }
  const planHash = typeof candidate.planHash === "string" ? candidate.planHash : "";
  if (!SHA256.test(planHash)) throw new Error("invalid_plan_hash");
  return { query: parseProvenanceQuery(params), planHash };
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  let query: ProvenanceQuery;
  try {
    query = parseProvenanceQuery(new URL(request.url).searchParams);
  } catch (error) {
    return response({ error: error instanceof Error ? error.message : "invalid_provenance_query", contract: RECOVERY_PROVENANCE_CONTRACT }, 400);
  }
  try {
    const plan = await buildRecoveryProvenancePlan(runtime, query);
    return response(plan as unknown as Record<string, unknown>);
  } catch {
    return response({ error: "recovery_provenance_unavailable", contract: RECOVERY_PROVENANCE_CONTRACT }, 503);
  }
}

/**
 * Metadata-only apply. The caller must opt into the deployment flag, provide
 * the exact preview plan hash, and send the fixed intent header. The handler
 * recomputes the bounded preview before writing, so a stale or reordered plan
 * cannot be applied. It never writes raw facts, releases, pointers, or flags.
 */
export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (request.headers.get("x-flow-recovery-provenance-intent") !== APPLY_INTENT) return response({ error: "repair_intent_required", contract: RECOVERY_PROVENANCE_CONTRACT }, 400);
  if (runtime.RECOVERY_PROVENANCE_REPAIR_ENABLED !== "1") return response({ error: "repair_disabled", contract: RECOVERY_PROVENANCE_CONTRACT }, 409);
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) return response({ error: "payload_too_large" }, 413);
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return response({ error: "payload_too_large" }, 413);
  let input: { query: ProvenanceQuery; planHash: string };
  try {
    input = queryFromBody(JSON.parse(body));
  } catch (error) {
    return response({ error: error instanceof Error ? error.message : "invalid_repair_request", contract: RECOVERY_PROVENANCE_CONTRACT }, 400);
  }
  try {
    const plan = await buildRecoveryProvenancePlan(runtime, input.query);
    if (plan.planHash !== input.planHash) return response({ error: "plan_hash_mismatch", expectedPlanHash: plan.planHash, contract: RECOVERY_PROVENANCE_CONTRACT }, 409);
    const applied = await applyRecoveryProvenancePlan(runtime, plan);
    return response({ contract: RECOVERY_PROVENANCE_CONTRACT, planHash: plan.planHash, status: "metadata_applied", applied, executionBlocked: true });
  } catch (error) {
    const code = error instanceof Error ? error.message : "repair_failed";
    const safe = new Set(["database_unavailable", "plan_not_applyable", "write_cap_exceeded"]);
    return response({ error: safe.has(code) ? code : "repair_failed", contract: RECOVERY_PROVENANCE_CONTRACT }, code === "database_unavailable" ? 503 : 409);
  }
}
