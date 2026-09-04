import { runtimeEnv } from "@/app/lib/dashboard-store";
import { getPipelineWindowRecovery, PipelineWindowError } from "@/app/lib/pipeline-window-recovery";
import { validateRecoveryReceipt } from "@/app/lib/recovery-receipt";

export const dynamic = "force-dynamic";

const SHA256 = /^[0-9a-f]{64}$/;
const RECEIPT_KEY = /^market-data\/recovery\/receipts\/[A-Za-z0-9._:-]{1,120}\/\d{4}-\d{2}-\d{2}\/[a-z_]{1,64}\/[0-9]{1,12}\.json$/;
const MAX_RECEIPT_BYTES = 256 * 1024;

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

export async function PUT(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!enabled(runtime.PIPELINE_RECOVERY_ENABLED)) return response({ error: "pipeline_recovery_disabled" }, 404);
  if (!runtime.DB || !runtime.RAW_BUCKET) return response({ error: "storage_unavailable" }, 503);
  const recoveryId = new URL(request.url).searchParams.get("recoveryId") ?? "";
  const key = request.headers.get("X-Flow-Recovery-Receipt-Key")?.trim() ?? "";
  const expectedSha = request.headers.get("X-Flow-Recovery-Receipt-Sha256")?.trim().toLowerCase() ?? "";
  const rawLength = request.headers.get("content-length");
  const length = rawLength === null ? null : Number(rawLength);
  if (!recoveryId || !RECEIPT_KEY.test(key) || key.includes("..") || key.includes("//") || !SHA256.test(expectedSha) || (length !== null && (!Number.isSafeInteger(length) || length <= 0 || length > MAX_RECEIPT_BYTES))) {
    return response({ error: "invalid_recovery_receipt_headers" }, 422);
  }
  try {
    const recovery = await getPipelineWindowRecovery(runtime.DB, recoveryId);
    if (!recovery) return response({ error: "recovery_not_found" }, 404);
    if (!key.startsWith(`market-data/recovery/receipts/${recoveryId}/`)) return response({ error: "receipt_recovery_mismatch" }, 409);
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength === 0 || bytes.byteLength > MAX_RECEIPT_BYTES) return response({ error: "receipt_object_size_invalid" }, 422);
    if (!secureEqual(await sha256Hex(bytes), expectedSha)) return response({ error: "receipt_sha256_mismatch" }, 409);
    let value: unknown;
    try {
      value = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return response({ error: "receipt_json_invalid" }, 422);
    }
    const attempt = value && typeof value === "object" && !Array.isArray(value) ? Number((value as Record<string, unknown>).attemptCount) : NaN;
    const validated = validateRecoveryReceipt(value, {
      recoveryId,
      runKey: recovery.run_key,
      planSha256: recovery.plan_sha256,
      attemptCount: attempt,
    });
    const expectedKey = `market-data/recovery/receipts/${recoveryId}/${validated.dataDate}/${validated.stage}/${validated.attemptCount}.json`;
    if (key !== expectedKey) return response({ error: "receipt_key_binding_mismatch" }, 409);
    const existing = await runtime.RAW_BUCKET.head(key);
    if (existing) {
      if (existing.customMetadata?.sha256 !== expectedSha) return response({ error: "immutable_receipt_conflict" }, 409);
      return response({ status: "stored", key, sha256: expectedSha, bytes: existing.size ?? bytes.byteLength, idempotent: true });
    }
    await runtime.RAW_BUCKET.put(key, bytes, {
      httpMetadata: { contentType: "application/json" },
      customMetadata: { sha256: expectedSha, recoveryId, runKey: recovery.run_key, attemptCount: String(validated.attemptCount) },
    } as unknown as R2PutOptions);
    return response({ status: "stored", key, sha256: expectedSha, bytes: bytes.byteLength, idempotent: false }, 201);
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    console.error("pipeline recovery receipt upload failed", error instanceof Error ? error.message : String(error));
    return response({ error: "pipeline_recovery_receipt_failed" }, 503);
  }
}
