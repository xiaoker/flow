import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  getPipelineWindowRecovery,
  PipelineWindowError,
  registerPipelineWindowRecovery,
  validatePipelineRecoveryRegistrationInput,
  type PipelineRecoveryRegistrationInput,
} from "@/app/lib/pipeline-window-recovery";
import { getPipelineWindow } from "@/app/lib/pipeline-window";
import {
  recoveryPlanObjectKey,
  storeImmutableRecoveryPlan,
  readRecoveryPlanObject,
  validateRecoveryPlanBytes,
  verifyArchiveProjectionRebind,
} from "@/app/lib/pipeline-recovery-plan";

export const dynamic = "force-dynamic";

// The request carries the exact plan bytes and the approved pre-recovery
// state as JSON strings. Escaping can nearly double their encoded size, so
// keep the outer envelope bounded without making the inner 300 KB plan limit
// impossible to satisfy.
const MAX_BODY_BYTES = 1_100_000;

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

function enabled(runtime: { PIPELINE_RECOVERY_ENABLED?: string }): boolean {
  return runtime.PIPELINE_RECOVERY_ENABLED === "1" || runtime.PIPELINE_RECOVERY_ENABLED === "true";
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
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new PipelineWindowError("invalid_json", 400);
  }
  if (!isRecord(parsed)) throw new PipelineWindowError("object_required", 400);
  return parsed;
}

function requiredString(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new PipelineWindowError(`missing_${key}`, 422);
  }
  return value;
}

function requiredInteger(body: Record<string, unknown>, key: string): number {
  const value = body[key];
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new PipelineWindowError(`invalid_${key}`, 422);
  }
  return Number(value);
}

function serialize(row: Record<string, unknown>): Record<string, unknown> {
  return {
    recoveryId: row.recovery_id,
    runKey: row.run_key,
    planKey: row.plan_key,
    planSha256: row.plan_sha256,
    scopeSha256: row.scope_sha256,
    expectedAttemptCount: row.expected_attempt_count,
    expectedUpdatedAt: row.expected_updated_at,
    previousStateJson: row.previous_state_json,
    reasonCode: row.reason_code,
    approvalReference: row.approval_reference,
    authorizedAt: row.authorized_at,
    expiresAt: row.expires_at,
    status: row.status,
    latestReceiptKey: row.latest_receipt_key,
    latestReceiptSha256: row.latest_receipt_sha256,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    completedAt: row.completed_at,
  };
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB) return response({ error: "database_unavailable" }, 503);
  const recoveryId = new URL(request.url).searchParams.get("recoveryId") ?? "";
  if (!recoveryId) return response({ error: "invalid_recovery_id" }, 422);
  try {
    const row = await getPipelineWindowRecovery(runtime.DB, recoveryId);
    if (!row) return response({ error: "recovery_not_found" }, 404);
    const pipeline = await getPipelineWindow(runtime.DB, row.run_key);
    if (!pipeline) return response({ error: "pipeline_window_not_found" }, 404);
    return response({
      ...serialize(row),
      pipeline: {
        runKey: pipeline.run_key,
        status: pipeline.status,
        leaseOwner: pipeline.lease_owner,
        leaseExpiresAt: pipeline.lease_expires_at,
        attemptCount: pipeline.attempt_count,
        sourceFingerprint: pipeline.source_fingerprint,
        githubRunId: pipeline.github_run_id,
        updatedAt: pipeline.updated_at,
      },
    });
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery status read failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_status_failed" }, 503);
  }
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB || !runtime.RAW_BUCKET) return response({ error: "storage_unavailable" }, 503);
  try {
    const body = await readBody(request);
    const recoveryId = requiredString(body, "recoveryId");
    const runKey = requiredString(body, "runKey");
    const planSha256 = requiredString(body, "planSha256");
    const scopeSha256 = requiredString(body, "scopeSha256");
    const planJson = requiredString(body, "planJson");
    const planKey = recoveryPlanObjectKey(recoveryId, scopeSha256);
    if (body.planKey !== undefined && body.planKey !== planKey) {
      throw new PipelineWindowError("invalid_plan_key", 422);
    }
    // Proof is never accepted from the request; derive it from immutable plans.
    if (body.archiveProjectionRebind !== undefined) {
      throw new PipelineWindowError("archive_rebind_client_proof_forbidden", 422);
    }
    const registration: PipelineRecoveryRegistrationInput = {
      recoveryId,
      runKey,
      planKey,
      planSha256,
      scopeSha256,
      expectedAttemptCount: requiredInteger(body, "expectedAttemptCount"),
      expectedUpdatedAt: requiredString(body, "expectedUpdatedAt"),
      previousStateJson: requiredString(body, "previousStateJson"),
      reasonCode: requiredString(body, "reasonCode"),
      approvalReference: requiredString(body, "approvalReference"),
      authorizedAt: requiredString(body, "authorizedAt"),
      expiresAt: requiredString(body, "expiresAt"),
      ...(body.supersedesRecoveryId === undefined || body.supersedesRecoveryId === ""
        ? {}
        : { supersedesRecoveryId: requiredString(body, "supersedesRecoveryId") }),
    };
    validatePipelineRecoveryRegistrationInput(registration);
    if (!(await getPipelineWindow(runtime.DB, runKey))) {
      throw new PipelineWindowError("pipeline_window_not_found", 404);
    }
    if (registration.reasonCode === "archive_projection_rebound") {
      if (!registration.supersedesRecoveryId) throw new PipelineWindowError("archive_rebind_previous_required", 422);
      const previous = await getPipelineWindowRecovery(runtime.DB, registration.supersedesRecoveryId);
      if (!previous || previous.run_key !== runKey || previous.status !== "BLOCKED") {
        throw new PipelineWindowError("archive_rebind_previous_invalid", 409);
      }
      const oldPlan = await readRecoveryPlanObject(runtime.RAW_BUCKET, previous.plan_key, {
        recoveryId: previous.recovery_id, runKey, planSha256: previous.plan_sha256,
        scopeSha256: previous.scope_sha256,
      });
      const newPlan = await validateRecoveryPlanBytes(new TextEncoder().encode(planJson), {
        recoveryId, runKey, planSha256, scopeSha256,
      }, 422);
      registration.archiveProjectionRebind = {
        previousPlanSha256: previous.plan_sha256,
        ...await verifyArchiveProjectionRebind(oldPlan, newPlan),
      };
    }
    const stored = await storeImmutableRecoveryPlan(
      runtime.RAW_BUCKET,
      new TextEncoder().encode(planJson),
      { recoveryId, runKey, planSha256, scopeSha256 },
    );
    const result = await registerPipelineWindowRecovery(runtime.DB, registration);
    return response({
      decision: result.decision,
      recoveryId: result.recovery.recovery_id,
      runKey: result.pipeline.run_key,
      status: result.recovery.status,
      pipelineStatus: result.pipeline.status,
      planKey: stored.key,
      planStorageDecision: stored.decision,
      supersededRecoveryId: result.supersededRecoveryId ?? null,
      rowsWritten: result.rowsWritten,
      rowsRead: result.rowsRead,
    }, result.decision === "registered" ? 201 : 200);
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery registration failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_registration_failed" }, 503);
  }
}
