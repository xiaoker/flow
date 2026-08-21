import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_BODY_BYTES = 1_000_000;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function bad(error: string, status = 400): Response {
  return Response.json({ error }, { status });
}

function artifact(value: unknown): value is { key: string; sha256: string; rows?: number; members?: number } {
  return isRecord(value) && typeof value.key === "string" && value.key.startsWith("market-data/pcf-v2/") && typeof value.sha256 === "string" && SHA256.test(value.sha256) &&
    (value.rows === undefined || (Number.isSafeInteger(value.rows) && Number(value.rows) >= 0)) &&
    (value.members === undefined || (Number.isSafeInteger(value.members) && Number(value.members) >= 0));
}

type ManifestArtifact = { key: string; sha256: string; rows?: number; members?: number };

async function readJson(request: Request): Promise<unknown> {
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) throw new Error("payload_too_large");
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) throw new Error("payload_too_large");
  return JSON.parse(body);
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return bad("unauthorized", 401);
  if (!runtime.DB || !runtime.RAW_BUCKET) return bad("storage_unavailable", 503);
  let value: unknown;
  try {
    value = await readJson(request);
  } catch (error) {
    return bad(error instanceof Error && error.message === "payload_too_large" ? "payload_too_large" : "invalid_json", error instanceof Error && error.message === "payload_too_large" ? 413 : 400);
  }
  if (!isRecord(value) || !isRecord(value.manifest)) return bad("invalid_pcf_manifest", 422);
  const manifest = value.manifest;
  const coverage = isRecord(manifest.coverage) ? manifest.coverage : null;
  const universe = isRecord(manifest.universe) ? manifest.universe : null;
  const artifacts = isRecord(manifest.artifacts) ? manifest.artifacts : null;
  const tradeDate = typeof manifest.tradeDate === "string" ? manifest.tradeDate : "";
  const jobId = typeof manifest.jobId === "string" ? manifest.jobId : "";
  const revisionId = typeof value.revisionId === "string" ? value.revisionId : "";
  const manifestKey = typeof value.manifestKey === "string" ? value.manifestKey : "";
  const manifestSha256 = typeof value.manifestSha256 === "string" ? value.manifestSha256 : "";
  if (manifest.schemaVersion !== 2 || manifest.dataset !== "sse_etf_pcf" || manifest.calculationVersion !== "pcf-capture-v2" || !ISO_DATE.test(tradeDate) || !jobId || !/^[a-f0-9]{24}$/.test(revisionId) || manifestKey !== `market-data/pcf-v2/manifests/${tradeDate}/${revisionId}.json` || !SHA256.test(manifestSha256) || !coverage || !universe || !artifacts || !artifact(artifacts.headers) || !artifact(artifacts.components) || !artifact(artifacts.data) || !artifact(artifacts.rawPack)) return bad("invalid_pcf_manifest", 422);
  const required = Number(universe.required);
  const captured = Number(coverage.captured);
  const valid = Number(coverage.valid);
  const failed = Number(coverage.failed);
  if (![required, captured, valid, failed].every((item) => Number.isSafeInteger(item) && item >= 0) || required !== captured || captured !== valid || failed !== 0) return bad("pcf_coverage_incomplete", 409);
  const canonicalArtifacts = {
    headers: artifacts.headers as ManifestArtifact,
    components: artifacts.components as ManifestArtifact,
    data: artifacts.data as ManifestArtifact,
    rawPack: artifacts.rawPack as ManifestArtifact,
  };

  try {
    const job = await runtime.DB.prepare("SELECT * FROM pcf_capture_job WHERE job_id = ? LIMIT 1").bind(jobId).first<Record<string, unknown>>();
    if (!job || job.target_date !== tradeDate) return bad("pcf_capture_job_not_found", 404);
    const artifactEntries = Object.values(canonicalArtifacts);
    for (const entry of artifactEntries) {
      const object = await runtime.RAW_BUCKET.head(String(entry.key));
      if (!object || object.customMetadata?.sha256 !== entry.sha256) return bad("pcf_artifact_not_verified", 409);
    }
    const manifestObject = await runtime.RAW_BUCKET.head(manifestKey);
    if (!manifestObject || manifestObject.customMetadata?.sha256 !== manifestSha256) return bad("pcf_manifest_not_verified", 409);
    const now = new Date().toISOString();
    const status = required === 0 ? "READY_NO_CHANGE" : "READY";
    const statements = [
      runtime.DB.prepare(
        `UPDATE pcf_capture_job
            SET status = ?, captured_count = ?, valid_count = ?, failed_count = ?, coverage_ppm = 1000000,
                canonical_manifest_key = ?, canonical_manifest_sha256 = ?, raw_pack_key = ?, raw_pack_sha256 = ?,
                completed_at = ?, updated_at = ?
          WHERE job_id = ?`,
      ).bind(status, captured, valid, failed, manifestKey, manifestSha256, canonicalArtifacts.rawPack.key, canonicalArtifacts.rawPack.sha256, now, now, jobId),
      ...(["headers", "components", "data"] as const).map((fileName) => runtime.DB!.prepare(
        `INSERT INTO history_artifact_pointer (source_id, dataset, trade_date, file_name, revision_id, object_key, sha256, content_encoding, row_count, observed_at, status, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'gzip', ?, ?, 'READY', ?)
         ON CONFLICT(source_id, dataset, trade_date, file_name) DO UPDATE SET
           revision_id = excluded.revision_id, object_key = excluded.object_key, sha256 = excluded.sha256,
           content_encoding = excluded.content_encoding, row_count = excluded.row_count, observed_at = excluded.observed_at,
           status = excluded.status, updated_at = excluded.updated_at
         WHERE excluded.observed_at >= history_artifact_pointer.observed_at`,
      ).bind("sse", "etf_pcf", tradeDate, fileName, revisionId, canonicalArtifacts[fileName].key, canonicalArtifacts[fileName].sha256, Number(canonicalArtifacts[fileName].rows || 0), String(manifest.observedAt || now), now)),
    ];
    await runtime.DB.batch(statements);
    return Response.json({ status, jobId, tradeDate, revisionId, pointerFiles: ["headers", "components", "data"] });
  } catch (error) {
    console.error("PCF canonical finalize failed", error);
    return bad("pcf_finalize_failed", 503);
  }
}
