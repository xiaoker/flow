import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  hasCompleteReadModelManifest,
  isReadModelManifest,
  READ_MODEL_NAME,
  RELEASE_ID,
} from "@/app/lib/read-model-contract";

export const dynamic = "force-dynamic";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function GET(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) return Response.json({ error: "storage_unavailable" }, { status: 503 });

  const url = new URL(request.url);
  const releaseId = url.searchParams.get("releaseId") ?? "";
  const name = url.searchParams.get("name") ?? "";
  if (!RELEASE_ID.test(releaseId) || !READ_MODEL_NAME.test(name)) {
    return Response.json({ error: "invalid_artifact_reference" }, { status: 400 });
  }

  try {
    const row = await runtime.DB
      .prepare(
        `SELECT release_id, as_of, status, manifest_json
         FROM release_manifest
         WHERE release_id = ? AND status IN ('ACTIVE', 'SUPERSEDED')
         LIMIT 1`,
      )
      .bind(releaseId)
      .first<{ release_id: string; as_of: string; status: string; manifest_json: string }>();
    if (!row) return Response.json({ error: "release_not_found" }, { status: 404 });
    const parsed: unknown = JSON.parse(row.manifest_json);
    if (!isReadModelManifest(parsed) || !hasCompleteReadModelManifest(parsed)) {
      return Response.json({ error: "invalid_manifest" }, { status: 503 });
    }
    const descriptor = parsed.artifacts[name];
    if (!descriptor) return Response.json({ error: "artifact_not_found" }, { status: 404 });
    const prefix = `market-data/releases/${row.as_of}/${releaseId}/`;
    if (!descriptor.key.startsWith(prefix)) return Response.json({ error: "artifact_key_mismatch" }, { status: 503 });

    const object = await runtime.RAW_BUCKET.get(descriptor.key);
    if (
      !object ||
      object.customMetadata?.asOf !== row.as_of ||
      object.customMetadata?.releaseId !== releaseId ||
      object.customMetadata?.sha256 !== descriptor.sha256 ||
      object.size !== descriptor.bytes
    ) {
      return Response.json({ error: "artifact_unavailable" }, { status: 503 });
    }

    const headers = new Headers({
      "Cache-Control": "private, no-store",
      "Content-Type": object.httpMetadata?.contentType ?? "application/json; charset=utf-8",
      "X-Flow-Release-Id": releaseId,
      "X-Flow-As-Of": row.as_of,
      "X-Flow-Artifact": name,
    });
    if (object.httpEtag) headers.set("ETag", object.httpEtag);
    return new Response(object.body, { headers });
  } catch (error) {
    console.error("exact release artifact read failed", error);
    return Response.json({ error: "artifact_unavailable" }, { status: 503 });
  }
}
