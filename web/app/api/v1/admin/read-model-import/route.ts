import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  isReadModelManifest,
  isRecord,
  READ_MODEL_NAME,
  type ReadModelManifest,
} from "@/app/lib/read-model-contract";
import { activateReadModels } from "../import/route";
import { candidateUploadAuthority, parseCandidateExecution } from "@/app/lib/candidate-upload-authority";
import { PipelineWindowError } from "@/app/lib/pipeline-window";
import { candidateManifestSha256 } from "@/app/lib/candidate-read";

export const dynamic = "force-dynamic";

// Each request contains one compressed read model. Keeping this endpoint
// intentionally small avoids making the free Worker parse the full dashboard
// plus every view model in one invocation.
const MAX_BODY_BYTES = 1_500_000;
const NAME = READ_MODEL_NAME;

type ReadModelArtifactUpload = {
  name: string;
  key: string;
  sha256: string;
  bytes: number;
  contentBase64: string;
  contentEncoding?: "gzip";
};

type ReadModelActivationMode = "active" | "stage_only";

type ReadModelUpload = {
  version: 1;
  activationMode?: ReadModelActivationMode;
  manifest: ReadModelManifest;
  artifact: ReadModelArtifactUpload;
  finalize?: boolean;
  pipelineExecution?: unknown;
  candidateManifestJson?: unknown;
  operation?: "inspect_artifact" | "upload_once";
};

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isManifest(value: unknown): value is ReadModelManifest {
  return isReadModelManifest(value);
}

function isUpload(value: unknown): value is ReadModelUpload {
  if (!isRecord(value) || value.version !== 1 || !isManifest(value.manifest) || !isRecord(value.artifact)) {
    return false;
  }
  const artifact = value.artifact;
  const name = artifact.name;
  const declared = typeof name === "string" ? value.manifest.artifacts[name] : undefined;
  return Boolean(
    typeof name === "string" &&
      NAME.test(name) &&
      isRecord(declared) &&
      artifact.key === declared.key &&
      artifact.sha256 === declared.sha256 &&
      artifact.bytes === declared.bytes &&
      typeof artifact.contentBase64 === "string" &&
      artifact.contentBase64.length > 0 &&
      (artifact.contentEncoding === undefined || artifact.contentEncoding === "gzip") &&
      (value.activationMode === undefined || value.activationMode === "active" || value.activationMode === "stage_only") &&
      (value.finalize === undefined || typeof value.finalize === "boolean") &&
      (value.operation === undefined || value.operation === "inspect_artifact" || value.operation === "upload_once"),
  );
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("invalid_base64");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function decodeContent(artifact: ReadModelArtifactUpload, bounded = false): Promise<Uint8Array> {
  const encoded = decodeBase64(artifact.contentBase64);
  if (artifact.contentEncoding !== "gzip") return encoded;
  const stream = new Blob([encoded]).stream().pipeThrough(new DecompressionStream("gzip"));
  if (bounded) {
    const reader = stream.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BODY_BYTES || chunks.length >= 512) throw new PipelineWindowError("candidate_content_too_large", 413);
        chunks.push(value);
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  }
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", content);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function putImmutable(
  bucket: R2Bucket,
  key: string,
  content: Uint8Array | string,
  sha256: string,
  options: R2PutOptions,
  fenced = false,
): Promise<void> {
  if (fenced) {
    await bucket.put(key, content, { ...options, onlyIf: new Headers({ "If-None-Match": "*" }) });
    const saved = await bucket.get(key);
    const expectedBytes = typeof content === "string" ? new TextEncoder().encode(content).byteLength : content.byteLength;
    if (!saved || saved.size !== expectedBytes) throw new PipelineWindowError("candidate_immutable_conflict", 409);
    const bytes = saved ? new Uint8Array(await saved.arrayBuffer()) : null;
    if (!saved || saved.customMetadata?.sha256 !== sha256 || !bytes || await sha256Hex(bytes) !== sha256) {
      throw new PipelineWindowError("candidate_immutable_conflict", 409);
    }
    return;
  }
  const existing = typeof bucket.head === "function" ? await bucket.head(key) : null;
  if (existing) {
    if (existing.customMetadata?.sha256 !== sha256) throw new Error(`read_model_immutable_conflict:${key}`);
    return;
  }
  await bucket.put(key, content, options);
}

async function allArtifactsPresent(bucket: R2Bucket, manifest: ReadModelManifest): Promise<boolean> {
  if (typeof bucket.head !== "function") return true;
  const checks = await Promise.all(
    Object.values(manifest.artifacts).map(async (artifact) => Boolean(await bucket.head(artifact.key))),
  );
  return checks.every(Boolean);
}

/**
 * Persist a complete candidate manifest without touching active_release.
 * This is the PR0 canary path: immutable R2 objects and a STAGED D1 row are
 * written, while all production readers continue to resolve the old pointer.
 */
export async function stageReadModels(
  db: D1Database,
  release: { releaseId: string; asOf: string; manifestKey: string; manifestSha256: string },
  manifest: ReadModelManifest,
  stagedAt: string,
): Promise<"staged_candidate"> {
  let observedAtColumnsReady = false;
  try {
    await db.prepare("SELECT observed_at FROM release_manifest LIMIT 1").first();
    await db.prepare("SELECT observed_at FROM active_release LIMIT 1").first();
    observedAtColumnsReady = true;
  } catch {
    // Keep the candidate path compatible with the pre-observed_at schema.
  }

  if (!observedAtColumnsReady) {
    await db
      .prepare(
        `INSERT INTO release_manifest (
           release_id, as_of, schema_version, manifest_sha256, manifest_json, status, created_at
         ) VALUES (?, ?, ?, ?, ?, 'STAGED', ?)
         ON CONFLICT(release_id) DO UPDATE SET
           as_of = excluded.as_of,
           schema_version = excluded.schema_version,
           manifest_sha256 = excluded.manifest_sha256,
           manifest_json = excluded.manifest_json,
           status = CASE
             WHEN release_manifest.status = 'ACTIVE' THEN 'ACTIVE'
             ELSE 'STAGED'
           END`,
      )
      .bind(
        release.releaseId,
        release.asOf,
        manifest.schemaVersion,
        release.manifestSha256,
        JSON.stringify(manifest),
        stagedAt,
      )
      .run();
  } else {
    await db
      .prepare(
        `INSERT INTO release_manifest (
           release_id, as_of, schema_version, manifest_sha256, manifest_json,
           observed_at, status, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, 'STAGED', ?)
         ON CONFLICT(release_id) DO UPDATE SET
           as_of = excluded.as_of,
           schema_version = excluded.schema_version,
           manifest_sha256 = excluded.manifest_sha256,
           manifest_json = excluded.manifest_json,
           observed_at = excluded.observed_at,
           status = CASE
             WHEN release_manifest.status = 'ACTIVE' THEN 'ACTIVE'
             ELSE 'STAGED'
           END`,
      )
      .bind(
        release.releaseId,
        release.asOf,
        manifest.schemaVersion,
        release.manifestSha256,
        JSON.stringify(manifest),
        manifest.observedAt ?? null,
        stagedAt,
      )
      .run();
  }
  return "staged_candidate";
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return NextResponse.json({ error: "storage_unavailable" }, { status: 503 });
  }
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (
    isRecord(value) &&
    isRecord(value.manifest) &&
    value.manifest.schemaVersion === 4 &&
    value.activationMode !== "stage_only"
  ) {
    return NextResponse.json({ error: "candidate_manifest_requires_stage_only" }, { status: 409 });
  }
  if (!isUpload(value)) return NextResponse.json({ error: "invalid_read_model_upload" }, { status: 422 });

  try {
    const authority = Object.hasOwn(value, "pipelineExecution")
      ? candidateUploadAuthority(runtime.DB, parseCandidateExecution(value.pipelineExecution, value.manifest, value.activationMode),
        () => new Date(), { bucket: runtime.RAW_BUCKET, enabled: runtime.PIPELINE_RECOVERY_ENABLED })
      : null;
    let exactManifestJson: string | undefined;
    if (Object.hasOwn(value, "candidateManifestJson")) {
      if (!authority || typeof value.candidateManifestJson !== "string"
          || new TextEncoder().encode(value.candidateManifestJson).byteLength > 65536) {
        throw new PipelineWindowError("invalid_candidate_manifest_bytes", 422);
      }
      let parsed: unknown;
      try { parsed = JSON.parse(value.candidateManifestJson); }
      catch { throw new PipelineWindowError("invalid_candidate_manifest_bytes", 422); }
      if (JSON.stringify(parsed) !== JSON.stringify(value.manifest)
          || await candidateManifestSha256(value.candidateManifestJson) !== value.manifest.manifestSha256) {
        throw new PipelineWindowError("candidate_manifest_bytes_mismatch", 409);
      }
      exactManifestJson = value.candidateManifestJson;
    }
    if (authority) await authority.confirm();
    if (authority && value.artifact.bytes > MAX_BODY_BYTES) throw new PipelineWindowError("candidate_content_too_large", 413);
    const content = await decodeContent(value.artifact, Boolean(authority));
    if (content.byteLength !== value.artifact.bytes) throw new Error("read_model_size_mismatch");
    if ((await sha256Hex(content)) !== value.artifact.sha256) throw new Error("read_model_sha256_mismatch");
    if (value.operation === "inspect_artifact") {
      if (!authority || !exactManifestJson || value.finalize !== false) {
        throw new PipelineWindowError("invalid_candidate_inspection", 422);
      }
      const saved = await authority.effect(() => runtime.RAW_BUCKET!.get(value.artifact.key));
      if (!saved) return NextResponse.json({ error: "candidate_artifact_not_found" }, { status: 404 });
      if (saved.size !== value.artifact.bytes || saved.size > MAX_BODY_BYTES ||
          saved.customMetadata?.releaseId !== value.manifest.releaseId ||
          saved.customMetadata?.asOf !== value.manifest.asOf ||
          saved.customMetadata?.sha256 !== value.artifact.sha256) {
        throw new PipelineWindowError("candidate_artifact_inspection_mismatch", 409);
      }
      const reader = saved.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const { done, value: chunk } = await reader.read();
          if (done) break;
          size += chunk.byteLength;
          if (size > MAX_BODY_BYTES || chunks.length >= 512) throw new PipelineWindowError("candidate_content_too_large", 413);
          chunks.push(chunk);
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      if (size !== content.byteLength || await sha256Hex(bytes) !== value.artifact.sha256) {
        throw new PipelineWindowError("candidate_artifact_inspection_mismatch", 409);
      }
      await authority.confirm();
      return NextResponse.json({ status: "verified_artifact", releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf, manifestSha256: value.manifest.manifestSha256,
        name: value.artifact.name, sha256: value.artifact.sha256, bytes: size },
        { headers: { "Cache-Control": "private, no-store, max-age=0" } });
    }
    if (value.operation === "upload_once") {
      if (!authority || !exactManifestJson || typeof value.finalize !== "boolean") {
        throw new PipelineWindowError("invalid_candidate_upload_once", 422);
      }
      // The key deliberately excludes lease owner/attempt: takeover must not
      // grant another effect for an immutable artifact. A claimed but unfinished
      // upload stays blocked; absence is not permission to reset this marker.
      const reference = JSON.stringify({ schemaVersion: 1, execution: value.pipelineExecution,
        releaseId: value.manifest.releaseId, manifestSha256: value.manifest.manifestSha256,
        artifact: { name: value.artifact.name, key: value.artifact.key,
          sha256: value.artifact.sha256, bytes: value.artifact.bytes }, finalize: value.finalize });
      if (new TextEncoder().encode(reference).byteLength > 4096) {
        throw new PipelineWindowError("candidate_upload_intent_too_large", 413);
      }
      const markerKey = `market-data/releases/${value.manifest.asOf}/${value.manifest.releaseId}/upload-intents/${value.artifact.name}.json`;
      const claimed = await authority.effect(() => runtime.RAW_BUCKET!.put(markerKey, reference, {
        onlyIf: new Headers({ "If-None-Match": "*" }),
        httpMetadata: { contentType: "application/json" },
      }));
      // Only a definite successful conditional put authorizes this invocation.
      // A timeout or pre-existing marker never authorizes an artifact write.
      if (!claimed) throw new PipelineWindowError("candidate_upload_intent_exists", 409);
    }
    const putArtifact = () => putImmutable(runtime.RAW_BUCKET!, value.artifact.key, content, value.artifact.sha256, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf,
        sha256: value.artifact.sha256,
      },
    }, Boolean(authority));
    if (authority) await authority.effect(putArtifact);
    else await putArtifact();

    if (value.finalize !== true) {
      return NextResponse.json({ status: "staged", asOf: value.manifest.asOf, name: value.artifact.name });
    }
    if (!(await allArtifactsPresent(runtime.RAW_BUCKET, value.manifest))) {
      return NextResponse.json({ status: "waiting_artifacts", asOf: value.manifest.asOf }, { status: 409 });
    }
    const manifestBody = exactManifestJson ?? JSON.stringify(value.manifest);
    const manifestBodySha256 = await sha256Hex(new TextEncoder().encode(manifestBody));
    const manifestKey = `market-data/releases/${value.manifest.asOf}/${value.manifest.releaseId}/manifest.json`;
    const putManifest = () => putImmutable(runtime.RAW_BUCKET!, manifestKey, manifestBody, manifestBodySha256, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf,
        sha256: manifestBodySha256,
      },
    }, Boolean(authority));
    if (authority) await authority.effect(putManifest);
    else await putManifest();
    const activatedAt = new Date().toISOString();
    const release = {
      releaseId: value.manifest.releaseId,
      asOf: value.manifest.asOf,
      manifestKey,
      manifestSha256: value.manifest.manifestSha256 ?? manifestBodySha256,
    };
    const activation = authority
      ? await authority.stage(release, value.manifest, manifestBody)
      : value.activationMode === "stage_only"
      ? await stageReadModels(runtime.DB, release, value.manifest, activatedAt)
      : await activateReadModels(runtime.DB, release, value.manifest, activatedAt);
    return NextResponse.json({
      status: activation,
      asOf: value.manifest.asOf,
      releaseId: value.manifest.releaseId,
      manifestKey,
    });
  } catch (error) {
    if (error instanceof PipelineWindowError) {
      return NextResponse.json({ error: error.message }, { status: error.status });
    }
    if (Object.hasOwn(value, "pipelineExecution")) {
      return NextResponse.json({ error: "candidate_upload_unavailable" }, { status: 503 });
    }
    console.error("read model import failed", error);
    return NextResponse.json(
      { error: "read_model_import_failed", detail: error instanceof Error ? error.message : "unknown_error" },
      { status: 500 },
    );
  }
}
