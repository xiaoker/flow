import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const SHA256 = /^[0-9a-f]{64}$/;
const MAX_ARTIFACT_BYTES = 20 * 1024 * 1024;
const KEY = /^market-data\/pcf-v2\/(canonical|raw-pack|manifests)\/[A-Za-z0-9._/-]{1,220}$/;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function bad(error: string, status = 400): Response {
  return Response.json({ error }, { status });
}

export async function PUT(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return bad("unauthorized", 401);
  if (!runtime.RAW_BUCKET) return bad("raw_bucket_unavailable", 503);
  if (!request.body) return bad("request_body_required");
  const key = request.headers.get("X-Flow-Artifact-Key")?.trim() ?? "";
  const sha256 = request.headers.get("X-Flow-Artifact-Sha256")?.trim().toLowerCase() ?? "";
  const contentLengthHeader = request.headers.get("content-length");
  const contentLength = Number(contentLengthHeader || 0);
  if (!KEY.test(key) || key.includes("..") || key.includes("//") || !SHA256.test(sha256) || !contentLengthHeader || !Number.isSafeInteger(contentLength) || contentLength <= 0 || contentLength > MAX_ARTIFACT_BYTES) {
    return bad("invalid_pcf_artifact_headers", 422);
  }
  try {
    const existing = await runtime.RAW_BUCKET.head(key);
    if (existing) {
      if (existing.customMetadata?.sha256 !== sha256) return bad("immutable_object_conflict", 409);
      return Response.json({ key, size: existing.size, idempotent: true });
    }
    const stored = await runtime.RAW_BUCKET.put(key, request.body, {
      httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" },
      customMetadata: { sha256, artifactKey: key },
      sha256,
    } as unknown as R2PutOptions);
    return Response.json({ key, size: stored?.size ?? contentLength, idempotent: false });
  } catch (error) {
    console.error("PCF canonical artifact upload failed", error);
    return bad("pcf_artifact_upload_failed", 503);
  }
}
