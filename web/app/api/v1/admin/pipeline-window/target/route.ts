import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  bindPipelineTargetDate,
  PipelineWindowError,
} from "@/app/lib/pipeline-window";

export const dynamic = "force-dynamic";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function response(body: Record<string, unknown>, status = 200): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "private, no-store" },
  });
}

function authorized(request: Request, expected: string): boolean {
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  return Boolean(expected && supplied && secureEqual(supplied, expected));
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!runtime.DB) return response({ error: "database_unavailable" }, 503);

  let value: Record<string, unknown>;
  try {
    const body = await request.json();
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("object_required");
    value = body as Record<string, unknown>;
  } catch {
    return response({ error: "invalid_json" }, 400);
  }

  const runKey = typeof value.runKey === "string" ? value.runKey : "";
  const leaseOwner = typeof value.leaseOwner === "string" ? value.leaseOwner : "";
  const targetDate = typeof value.targetDate === "string" ? value.targetDate : "";
  const sourceFingerprint = value.sourceFingerprint === null
    ? null
    : typeof value.sourceFingerprint === "string" ? value.sourceFingerprint : undefined;
  if (!runKey || !leaseOwner || !targetDate) {
    return response({ error: "invalid_target_binding_request" }, 422);
  }

  try {
    const result = await bindPipelineTargetDate(runtime.DB, {
      runKey,
      leaseOwner,
      targetDate,
      sourceFingerprint,
    });
    return response({
      runKey: result.runKey,
      decision: result.decision,
      status: result.status,
      targetDate: result.targetDate,
      leaseOwner: result.leaseOwner,
      leaseExpiresAt: result.leaseExpiresAt,
      rowsRead: result.rowsRead,
      rowsWritten: result.rowsWritten,
    });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline window target binding failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_window_target_binding_failed" }, 503);
  }
}
