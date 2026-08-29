import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  getPipelineWindow,
  markPipelineWindow,
  PipelineWindowError,
  type PipelineWindowStatus,
} from "@/app/lib/pipeline-window";

export const dynamic = "force-dynamic";

const ALLOWED_STATUSES: PipelineWindowStatus[] = [
  "DISPATCHED",
  "RUNNING",
  "COMPLETED",
  "FAILED_RETRYABLE",
  "BLOCKED_QUALITY",
];

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

function isAuthorized(request: Request, expected: string): boolean {
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  return Boolean(expected && supplied && secureEqual(supplied, expected));
}

function serialize(row: Record<string, unknown>): Record<string, unknown> {
  return {
    runKey: row.run_key,
    pipeline: row.pipeline,
    windowDate: row.window_date,
    windowName: row.window_name,
    contractVersion: row.contract_version,
    targetDate: row.target_date,
    status: row.status,
    leaseOwner: row.lease_owner,
    leaseExpiresAt: row.lease_expires_at,
    sourceFingerprint: row.source_fingerprint,
    githubRunId: row.github_run_id,
    attemptCount: row.attempt_count,
    lastErrorCode: row.last_error_code,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    completedAt: row.completed_at,
  };
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!isAuthorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!runtime.DB) return response({ error: "database_unavailable" }, 503);
  const runKey = new URL(request.url).searchParams.get("runKey") ?? "";
  if (!runKey) return response({ error: "invalid_run_key" }, 422);
  try {
    const row = await getPipelineWindow(runtime.DB, runKey);
    if (!row) return response({ error: "pipeline_window_not_found" }, 404);
    return response(serialize(row));
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline window status read failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_window_status_failed" }, 503);
  }
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!isAuthorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
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
  const statusValue = typeof value.status === "string" ? value.status : "";
  const errorCode = value.errorCode === null
    ? null
    : typeof value.errorCode === "string" ? value.errorCode : undefined;
  const sourceFingerprint = value.sourceFingerprint === null
    ? null
    : typeof value.sourceFingerprint === "string" ? value.sourceFingerprint : undefined;
  const githubRunId = value.githubRunId === null
    ? null
    : typeof value.githubRunId === "string" ? value.githubRunId : undefined;
  if (!runKey || !leaseOwner || !ALLOWED_STATUSES.includes(statusValue as PipelineWindowStatus)) {
    return response({ error: "invalid_status_request" }, 422);
  }
  const status = statusValue as PipelineWindowStatus;
  try {
    const row = await markPipelineWindow(runtime.DB, {
      runKey,
      leaseOwner,
      status,
      errorCode,
      sourceFingerprint,
      githubRunId,
    });
    return response(serialize(row));
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline window status update failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_window_status_failed" }, 503);
  }
}
