import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  parseR2ReleaseMetadataRepairInput,
  repairR2ReleaseMetadata,
  R2_RELEASE_METADATA_REPAIR_MAX_INPUT_BYTES,
} from "@/app/lib/r2-release-metadata-repair";

export const dynamic = "force-dynamic";

const REPAIR_INTENT = "release-metadata-sha256-v1";

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

/**
 * One-object legacy R2 metadata verifier/repair. Dry-run is read-only and is
 * available with the existing admin auth; same-key writes additionally require
 * the explicit R2_METADATA_REPAIR_ENABLED=1 deployment variable. This endpoint
 * never writes D1, alters release pointers, dispatches workflows, or changes
 * flags.
 */
export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return response({ error: "unauthorized" }, 401);
  }
  if (request.headers.get("x-flow-r2-repair-intent") !== REPAIR_INTENT) {
    return response({ error: "repair_intent_required" }, 400);
  }
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > R2_RELEASE_METADATA_REPAIR_MAX_INPUT_BYTES) return response({ error: "payload_too_large" }, 413);
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > R2_RELEASE_METADATA_REPAIR_MAX_INPUT_BYTES) return response({ error: "payload_too_large" }, 413);
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return response({ error: "invalid_json" }, 400);
  }
  let input;
  try {
    input = parseR2ReleaseMetadataRepairInput(value);
  } catch (error) {
    return response({ error: error instanceof Error ? error.message : "invalid_repair_request" }, 400);
  }
  try {
    const result = await repairR2ReleaseMetadata(runtime, input);
    return response(result as unknown as Record<string, unknown>);
  } catch (error) {
    const code = error instanceof Error ? error.message : "repair_failed";
    const status = code === "r2_object_missing" || code === "release_not_found"
      ? 404
      : code === "storage_unavailable" || code === "r2_repair_verification_failed"
        ? 503
        : 409;
    const safeCodes = new Set([
      "storage_unavailable",
      "repair_disabled",
      "release_not_found",
      "release_status_invalid",
      "manifest_invalid",
      "manifest_identity_mismatch",
      "artifact_not_declared",
      "artifact_key_invalid",
      "artifact_sha256_invalid",
      "artifact_size_invalid",
      "r2_object_missing",
      "r2_release_metadata_mismatch",
      "r2_as_of_metadata_mismatch",
      "r2_hash_mismatch",
      "r2_size_mismatch",
      "r2_body_hash_mismatch",
      "r2_repair_verification_failed",
    ]);
    return response({ error: safeCodes.has(code) ? code : "repair_failed", contract: "r2-release-metadata-repair-v1" }, status);
  }
}
