import { runtimeEnv } from "@/app/lib/dashboard-store";
import { candidateManifestSha256 } from "@/app/lib/candidate-read";
import { isReadModelManifest } from "@/app/lib/read-model-contract";
import { candidateUploadAuthority, parseCandidateExecution } from "@/app/lib/candidate-upload-authority";
import { bindRollingCheckpoint } from "@/app/lib/rolling-checkpoint-binding";
import { PipelineWindowError } from "@/app/lib/pipeline-window";

export const dynamic = "force-dynamic";

const ETF_FLOW_STATE_POINTER_KEY =
  "market-data/derived/etf-flow-state/2026-07-v3/latest.json";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isStatePointer(value: unknown): value is {
  version: 1;
  classificationVersion: string;
  objectKey: string;
  sha256: string;
  asOf: string;
  dateCount: number;
  updatedAt: string;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const pointer = value as Record<string, unknown>;
  return Boolean(
    pointer.version === 1 &&
      typeof pointer.classificationVersion === "string" &&
      pointer.classificationVersion === "2026-07-v3" &&
      typeof pointer.objectKey === "string" &&
      pointer.objectKey.startsWith("market-data/derived/etf-flow-state/") &&
      typeof pointer.sha256 === "string" &&
      /^[a-f0-9]{64}$/.test(pointer.sha256) &&
      typeof pointer.asOf === "string" &&
      /^\d{4}-\d{2}-\d{2}$/.test(pointer.asOf) &&
      Number.isInteger(pointer.dateCount) &&
      Number(pointer.dateCount) > 0 &&
      typeof pointer.updatedAt === "string",
  );
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const expectedKey = runtime.INGEST_API_KEY;
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!expectedKey || !supplied || !secureEqual(supplied, expectedKey)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.RAW_BUCKET) {
    return Response.json({ error: "raw_bucket_unavailable" }, { status: 503 });
  }

  try {
    if (new URL(request.url).searchParams.get("kind") === "rolling-checkpoint") {
      if (!runtime.DB) return Response.json({ error: "storage_unavailable" }, { status: 503 });
      const row = await runtime.DB.prepare(`SELECT m.release_id,m.as_of,m.manifest_sha256,m.manifest_json
        FROM active_release a JOIN release_manifest m ON m.release_id=a.release_id
        WHERE a.slot='dashboard' AND m.status='ACTIVE' AND m.as_of=a.as_of LIMIT 1`)
        .first<{release_id: string; as_of: string; manifest_sha256: string; manifest_json: string}>();
      if (!row) return Response.json({ error: "rolling_checkpoint_not_found" }, { status: 404 });
      const manifest: unknown = JSON.parse(row.manifest_json);
      if (!isReadModelManifest(manifest) || (manifest.slot ?? "dashboard") !== "dashboard" ||
          manifest.releaseId !== row.release_id || manifest.asOf !== row.as_of ||
          await candidateManifestSha256(row.manifest_json) !== row.manifest_sha256) {
        return Response.json({ error: "rolling_checkpoint_manifest_invalid" }, { status: 409 });
      }
      if (manifest.rollingPublication?.status !== "READY") {
        return Response.json({ error: "rolling_checkpoint_not_found" }, { status: 404 });
      }
      const prefix = `market-data/releases/${row.as_of}/${row.release_id}/rolling-checkpoint/`;
      const binding = await runtime.RAW_BUCKET.get(`${prefix}${row.manifest_sha256}.json`);
      if (!binding) return Response.json({ error: "rolling_checkpoint_not_found" }, { status: 409 });
      if (binding.size > 2048) return Response.json({ error: "rolling_checkpoint_binding_invalid" }, { status: 409 });
      const ref = JSON.parse(await binding.text());
      if (!ref || ref.version !== 1 || ref.releaseId !== row.release_id ||
          ref.asOf !== row.as_of || ref.manifestSha256 !== row.manifest_sha256 ||
          typeof ref.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(ref.sha256) ||
          !Number.isInteger(ref.bytes) || ref.bytes <= 0 || ref.bytes > 2_000_000) {
        return Response.json({ error: "rolling_checkpoint_binding_invalid" }, { status: 409 });
      }
      if (manifest.rollingPublication?.status !== "READY" || manifest.rollingPublication.checkpointSha256 !== ref.sha256) {
        return Response.json({ error: "rolling_checkpoint_publication_mismatch" }, { status: 409 });
      }
      const object = await runtime.RAW_BUCKET.get(
        `${prefix}${ref.sha256}.checkpoint.gz`,
      );
      if (!object?.body || object.size !== ref.bytes ||
          object.customMetadata?.sha256 !== ref.sha256 ||
          object.customMetadata?.asOf !== row.as_of ||
          object.customMetadata?.releaseId !== row.release_id) {
        return Response.json({ error: "rolling_checkpoint_unavailable" }, { status: 409 });
      }
      return new Response(object.body, { headers: {
        "Content-Type": "application/gzip", "Cache-Control": "private, no-store",
        "X-Flow-Release-Id": row.release_id, "X-Flow-As-Of": row.as_of,
        "X-Flow-Manifest-Sha256": row.manifest_sha256, "X-Flow-Checkpoint-Sha256": ref.sha256,
      } });
    }
    const pointerObject = await runtime.RAW_BUCKET.get(ETF_FLOW_STATE_POINTER_KEY);
    if (!pointerObject) {
      return Response.json({ error: "etf_state_pointer_not_found" }, { status: 404 });
    }
    const pointer = JSON.parse(await pointerObject.text()) as unknown;
    if (!isStatePointer(pointer)) {
      return Response.json({ error: "invalid_etf_state_pointer" }, { status: 500 });
    }
    const stateObject = await runtime.RAW_BUCKET.get(pointer.objectKey);
    if (!stateObject?.body) {
      return Response.json({ error: "etf_state_not_found" }, { status: 404 });
    }
    return new Response(stateObject.body, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-flow-state-as-of": pointer.asOf,
        "x-flow-state-sha256": pointer.sha256,
        "x-flow-state-date-count": String(pointer.dateCount),
      },
    });
  } catch (error) {
    console.error("etf state export failed", error);
    return Response.json(
      {
        error: "etf_state_export_failed",
        detail: error instanceof Error ? error.message : "unknown_etf_state_export_error",
      },
      { status: 500 },
    );
  }
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) return Response.json({ error: "storage_unavailable" }, { status: 503 });
  try {
    const reader = request.body?.getReader();
    if (!reader) return Response.json({ error: "invalid_body" }, { status: 422 });
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        size += part.value.byteLength;
        if (size > 2_800_000 || chunks.length >= 128) throw new PipelineWindowError("checkpoint_body_too_large", 413);
        chunks.push(part.value);
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    const input = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { input.set(chunk, offset); offset += chunk.byteLength; }
    const value = JSON.parse(new TextDecoder().decode(input));
    if (!value || typeof value.releaseId !== "string" || typeof value.manifestSha256 !== "string" ||
        typeof value.sha256 !== "string" || typeof value.contentBase64 !== "string" ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(value.contentBase64)) {
      throw new PipelineWindowError("invalid_checkpoint_binding", 422);
    }
    const db = runtime.DB;
    const bucket = runtime.RAW_BUCKET;
    const readActive = async () => {
      const row = await db.prepare(`SELECT m.release_id,m.as_of,m.manifest_sha256,m.manifest_json
        FROM active_release a JOIN release_manifest m ON m.release_id=a.release_id
        WHERE a.slot='dashboard' AND m.status='ACTIVE' AND m.as_of=a.as_of LIMIT 1`)
        .first<{release_id: string; as_of: string; manifest_sha256: string; manifest_json: string}>();
      if (!row) return null;
      const manifest: unknown = JSON.parse(row.manifest_json);
      if (!isReadModelManifest(manifest) || manifest.schemaVersion !== 3 ||
          (manifest.slot ?? "dashboard") !== "dashboard" ||
          manifest.releaseId !== row.release_id || manifest.asOf !== row.as_of ||
          await candidateManifestSha256(row.manifest_json) !== row.manifest_sha256) {
        throw new PipelineWindowError("checkpoint_active_manifest_invalid", 409);
      }
      return { row, manifest };
    };
    const active = await readActive();
    if (!active || value.releaseId !== active.row.release_id || value.manifestSha256 !== active.row.manifest_sha256) {
      throw new PipelineWindowError("checkpoint_active_changed", 409);
    }
    if (active.manifest.rollingPublication?.status !== "READY" || active.manifest.rollingPublication.checkpointSha256 !== value.sha256) {
      throw new PipelineWindowError("checkpoint_publication_mismatch", 409);
    }
    const execution = parseCandidateExecution(value.pipelineExecution, active.manifest, "stage_only", "active_checkpoint");
    const authority = candidateUploadAuthority(db, execution, () => new Date(),
      { bucket, enabled: runtime.PIPELINE_RECOVERY_ENABLED });
    await authority.confirm();
    const binary = atob(value.contentBase64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    if (bytes.byteLength > 2_000_000) throw new PipelineWindowError("checkpoint_body_too_large", 413);
    if (bytes.length < 3 || bytes[0] !== 0x1f || bytes[1] !== 0x8b || bytes[2] !== 8) {
      throw new PipelineWindowError("checkpoint_format_invalid", 422);
    }
    // Native publication recomputes and validates the bounded decoded cache
    // offline. This route fences and hashes the exact compressed blob already
    // declared by that active manifest; never inflate ~15MB in a Worker.
    // Every consumer independently decodes with the 20MB bound and inner hashes.
    const expected = { releaseId: active.row.release_id, asOf: active.row.as_of,
      manifestSha256: active.row.manifest_sha256 };
    const result = await bindRollingCheckpoint({
      expected, bytes, sha256: value.sha256,
      readActiveDashboard: async () => {
        await authority.confirm();
        const current = await readActive();
        return current ? { releaseId: current.row.release_id, asOf: current.row.as_of,
          manifestSha256: current.row.manifest_sha256 } : null;
      },
      putImmutable: async (key, content, metadata) => {
        await authority.effect(async () => {
          const saved = await bucket.get(key);
          if (saved) {
            const actual = new Uint8Array(await saved.arrayBuffer());
            if (actual.length !== content.length || actual.some((b, i) => b !== content[i])) {
              throw new PipelineWindowError("checkpoint_immutable_conflict", 409);
            }
            return;
          }
          const stored = await bucket.put(key, content, { onlyIf: new Headers({ "If-None-Match": "*" }),
            customMetadata: metadata });
          if (!stored) throw new PipelineWindowError("checkpoint_write_unknown", 409);
        });
      },
    });
    return Response.json({ status: "checkpoint_bound", ...result });
  } catch (error) {
    const status = error instanceof PipelineWindowError ? error.status : 409;
    return Response.json({ error: error instanceof Error && /^[a-z_]+$/.test(error.message)
      ? error.message : "checkpoint_binding_failed" }, { status });
  }
}
