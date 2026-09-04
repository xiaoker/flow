import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  claimPipelineWindowRecovery,
  PipelineWindowError,
} from "@/app/lib/pipeline-window-recovery";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 64_000;

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

function enabled(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

async function readBody(request: Request): Promise<Record<string, unknown>> {
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > MAX_BODY_BYTES) throw new PipelineWindowError("payload_too_large", 413);
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > MAX_BODY_BYTES) {
    throw new PipelineWindowError("payload_too_large", 413);
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new PipelineWindowError("invalid_json", 400);
  }
  if (!isRecord(value)) throw new PipelineWindowError("object_required", 400);
  return value;
}

function requiredString(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new PipelineWindowError(`missing_${key}`, 422);
  }
  return value;
}

function optionalString(body: Record<string, unknown>, key: string): string | null | undefined {
  const value = body[key];
  if (value === undefined || value === null) return value;
  if (typeof value !== "string") throw new PipelineWindowError(`invalid_${key}`, 422);
  return value;
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime.PIPELINE_RECOVERY_ENABLED)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB) return response({ error: "database_unavailable" }, 503);
  try {
    const body = await readBody(request);
    const result = await claimPipelineWindowRecovery(runtime.DB, {
      recoveryId: requiredString(body, "recoveryId"),
      runKey: requiredString(body, "runKey"),
      leaseOwner: requiredString(body, "leaseOwner"),
      sourceFingerprint: optionalString(body, "sourceFingerprint"),
      githubRunId: optionalString(body, "githubRunId"),
    });
    return response({
      recoveryId: result.recoveryId,
      runKey: result.runKey,
      decision: result.decision,
      recoveryStatus: result.recoveryStatus,
      status: result.status,
      leaseOwner: result.leaseOwner,
      leaseExpiresAt: result.leaseExpiresAt,
      attemptCount: result.attemptCount,
      rowsWritten: result.rowsWritten,
    });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery claim failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_claim_failed" }, 503);
  }
}
