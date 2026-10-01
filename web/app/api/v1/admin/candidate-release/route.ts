import { runtimeEnv } from "@/app/lib/dashboard-store";
import { verifyCloudflareAccessRequest } from "@/app/lib/cloudflare-access";
import {
  isCandidateArtifactName,
  isValidCandidateRequest,
  parseStagedCandidate,
  type CandidateManifestRow,
} from "@/app/lib/candidate-read";

export const dynamic = "force-dynamic";

function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "private, no-store, max-age=0" } });
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const access = await verifyCloudflareAccessRequest(request, {
    issuer: runtime.CANDIDATE_ACCESS_ISSUER,
    audience: runtime.CANDIDATE_ACCESS_AUD,
  });
  if (!access.authorized) {
    const unavailable = access.reason === "misconfigured" || access.reason === "verifier_unavailable";
    return json({ error: unavailable ? "access_verification_unavailable" : "unauthorized" }, unavailable ? 503 : 401);
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) return json({ error: "storage_unavailable" }, 503);

  const url = new URL(request.url);
  const releaseId = url.searchParams.get("releaseId") ?? "";
  const expectedManifestSha256 = url.searchParams.get("manifestSha256") ?? undefined;
  const artifactName = url.searchParams.get("artifact") ?? undefined;
  if (!isValidCandidateRequest(releaseId, expectedManifestSha256) ||
      (artifactName !== undefined && !isCandidateArtifactName(artifactName))) {
    return json({ error: "invalid_candidate_reference" }, 400);
  }
  if (artifactName !== undefined && expectedManifestSha256 === undefined) {
    return json({ error: "manifest_sha256_required" }, 400);
  }

  try {
    // release_id is the primary key: the read is a single-row exact lookup,
    // never a scan or enumeration of staged candidates.
    const row = await runtime.DB.prepare(
      `SELECT release_id, as_of, manifest_sha256, schema_version, status, manifest_json
         FROM release_manifest
        WHERE release_id = ? AND status = 'STAGED'
        LIMIT 1`,
    ).bind(releaseId).first<CandidateManifestRow>();
    if (!row) return json({ error: "staged_candidate_not_found" }, 404);
    if (expectedManifestSha256 !== undefined && expectedManifestSha256 !== row.manifest_sha256) {
      return json({ error: "candidate_manifest_changed" }, 409);
    }
    const manifest = await parseStagedCandidate(row);

    if (artifactName === undefined) {
      return json({
        releaseId: row.release_id,
        asOf: row.as_of,
        manifestSha256: row.manifest_sha256,
        status: row.status,
        slot: manifest.slot ?? "dashboard",
        manifest,
      });
    }

    const descriptor = manifest.artifacts[artifactName];
    if (!descriptor) return json({ error: "candidate_artifact_not_found" }, 404);
    const keyPrefix = `market-data/releases/${row.as_of}/${row.release_id}/`;
    if (!descriptor.key.startsWith(keyPrefix)) return json({ error: "candidate_artifact_key_mismatch" }, 503);
    const object = await runtime.RAW_BUCKET.get(descriptor.key);
    if (!object || object.size !== descriptor.bytes || object.size > 5_000_000 ||
        object.customMetadata?.asOf !== row.as_of ||
        object.customMetadata?.releaseId !== row.release_id ||
        object.customMetadata?.sha256 !== descriptor.sha256) {
      return json({ error: "candidate_artifact_unavailable" }, 503);
    }
    const headers = new Headers({
      "Cache-Control": "private, no-store, max-age=0",
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
      "X-Flow-Release-Id": row.release_id,
      "X-Flow-As-Of": row.as_of,
      "X-Flow-Release-Status": row.status,
      "X-Flow-Release-Slot": manifest.slot ?? "dashboard",
      "X-Flow-Manifest-Sha256": row.manifest_sha256,
      "X-Flow-Artifact": artifactName,
      "X-Flow-Artifact-Sha256": descriptor.sha256,
      "X-Flow-Artifact-Bytes": String(descriptor.bytes),
    });
    if (object.httpEtag) headers.set("ETag", object.httpEtag);
    return new Response(object.body, { headers });
  } catch (error) {
    const suppliedCode = error instanceof Error ? error.message : "";
    const code = /^candidate_[a-z0-9_]{1,80}$/.test(suppliedCode) ? suppliedCode : "candidate_read_failed";
    console.error("staged candidate read failed", { code, releaseId });
    return json({ error: code }, 503);
  }
}
