import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  getPipelineWindowRecovery,
  PipelineWindowError,
} from "@/app/lib/pipeline-window-recovery";
import { readRecoveryPlanObject } from "@/app/lib/pipeline-recovery-plan";

export const dynamic = "force-dynamic";

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
    const plan = await readRecoveryPlanObject(runtime.RAW_BUCKET, key, {
      recoveryId: recovery.recovery_id,
      runKey: recovery.run_key,
      planSha256: recovery.plan_sha256,
      scopeSha256: recovery.scope_sha256,
    });
    return response({ recoveryId, planKey: key, planSha256: recovery.plan_sha256, scopeSha256: recovery.scope_sha256, plan });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery plan read failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_plan_failed" }, 503);
  }
}
