import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  markPipelineWindowRecovery,
  PipelineWindowError,
  type PipelineRecoveryMarkInput,
  getPipelineWindowRecovery,
} from "@/app/lib/pipeline-window-recovery";
import { verifyRecoveryReceipt } from "@/app/lib/recovery-receipt";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 64_000;
const ALLOWED_STATUSES: PipelineRecoveryMarkInput["status"][] = [
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

function optionalBoolean(body: Record<string, unknown>, key: string): boolean | undefined {
  const value = body[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "boolean") throw new PipelineWindowError(`invalid_${key}`, 422);
  return value;
}

function requiredInteger(body: Record<string, unknown>, key: string): number {
  const value = body[key];
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new PipelineWindowError(`invalid_${key}`, 422);
  }
  return Number(value);
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime.PIPELINE_RECOVERY_ENABLED)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB) return response({ error: "database_unavailable" }, 503);
  try {
    const body = await readBody(request);
    const status = requiredString(body, "status") as PipelineRecoveryMarkInput["status"];
    if (!ALLOWED_STATUSES.includes(status)) throw new PipelineWindowError("invalid_status", 422);
    const recoveryId = requiredString(body, "recoveryId");
    const runKey = requiredString(body, "runKey");
    const expectedAttemptCount = requiredInteger(body, "expectedAttemptCount");
    const latestReceiptKey = optionalString(body, "latestReceiptKey");
    const latestReceiptSha256 = optionalString(body, "latestReceiptSha256");
    const renewLease = optionalBoolean(body, "renewLease");
    if (status === "COMPLETED" && (!latestReceiptKey || !latestReceiptSha256)) {
      throw new PipelineWindowError("completion_receipt_required", 422);
    }
    if ((latestReceiptKey && !latestReceiptSha256) || (!latestReceiptKey && latestReceiptSha256)) {
      throw new PipelineWindowError("completion_receipt_pair_required", 422);
    }
    if (latestReceiptKey && latestReceiptSha256) {
      if (!runtime.RAW_BUCKET) throw new PipelineWindowError("receipt_storage_unavailable", 503);
      const recovery = await getPipelineWindowRecovery(runtime.DB, recoveryId);
      if (!recovery) throw new PipelineWindowError("recovery_not_found", 404);
      await verifyRecoveryReceipt(runtime.RAW_BUCKET, latestReceiptKey, latestReceiptSha256, {
        recoveryId,
        runKey,
        planSha256: recovery.plan_sha256,
        attemptCount: expectedAttemptCount,
      });
    }
    const result = await markPipelineWindowRecovery(runtime.DB, {
      recoveryId,
      runKey,
      leaseOwner: requiredString(body, "leaseOwner"),
      expectedAttemptCount,
      status,
      errorCode: optionalString(body, "errorCode"),
      sourceFingerprint: optionalString(body, "sourceFingerprint"),
      githubRunId: optionalString(body, "githubRunId"),
      latestReceiptKey,
      latestReceiptSha256,
      renewLease,
    });
    return response({
      recoveryId: result.recovery.recovery_id,
      runKey: result.row.run_key,
      recoveryStatus: result.recovery.status,
      status: result.row.status,
      leaseOwner: result.row.lease_owner,
      leaseExpiresAt: result.row.lease_expires_at,
      attemptCount: result.row.attempt_count,
      rowsWritten: result.rowsWritten,
      rowsRead: result.rowsRead,
    });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery status update failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_status_failed" }, 503);
  }
}
