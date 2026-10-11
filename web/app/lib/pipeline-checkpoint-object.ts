/** Candidate exact-key checkpoint transport. No listing, lease mutation or release writes. */
import { getPipelineWindow, parsePipelineRunKey, PipelineWindowError } from "./pipeline-window.ts";
import { readRecoveryPlanObject, recoveryPlanObjectKey } from "./pipeline-recovery-plan.ts";

export const CHECKPOINT_OBJECT_MAX_BYTES = 1024 * 1024;
const HASH = /^[0-9a-f]{64}$/;
const SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const STAGES = new Set(["core_collection", "normalize", "build", "verify", "publish", "verify_visible", "handoff"]);
type DB = Parameters<typeof getPipelineWindow>[0] & { withSession?(constraint: string): DB };
type Bucket = {
  get(key: string): Promise<{ size: number; customMetadata?: Record<string, string>; body: ReadableStream<Uint8Array> } | null>;
  put(key: string, value: Uint8Array, options: { onlyIf: Headers; sha256: string; customMetadata: Record<string, string> }): Promise<unknown>;
};
type PlanBucket = Parameters<typeof readRecoveryPlanObject>[0];
type Runtime = { DB?: DB; RAW_BUCKET?: Bucket; INGEST_API_KEY?: string; PIPELINE_RECOVERY_ENABLED?: string };

function response(value: Record<string, unknown>, status = 200) {
  return Response.json(value, { status, headers: { "Cache-Control": "private, no-store" } });
}
function authorized(request: Request, expected: string) {
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!expected || expected.length !== supplied.length) return false;
  let difference = 0;
  for (let i = 0; i < expected.length; i++) difference |= expected.charCodeAt(i) ^ supplied.charCodeAt(i);
  return difference === 0;
}
async function hash(bytes: Uint8Array) {
  const digest = await crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function boundedBytes(stream: ReadableStream<Uint8Array> | null, maximum = CHECKPOINT_OBJECT_MAX_BYTES) {
  if (!stream) throw new PipelineWindowError("checkpoint_body_missing", 422);
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  let count = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (++count > 512) throw new PipelineWindowError("checkpoint_chunk_limit", 413);
      size += value.byteLength;
      if (size > maximum) throw new PipelineWindowError("checkpoint_object_too_large", 413);
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (!size) throw new PipelineWindowError("checkpoint_body_missing", 422);
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

export async function checkpointObjectRequest(request: Request, runtime: Runtime, now = () => new Date()): Promise<Response> {
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!runtime.DB || !runtime.RAW_BUCKET) return response({ error: "storage_unavailable" }, 503);
  if (!["GET", "POST"].includes(request.method)) return response({ error: "method_not_allowed" }, 405);
  const query = new URL(request.url).searchParams;
  const runKey = query.get("runKey") ?? "";
  const stage = query.get("stage") ?? "";
  const input = query.get("inputSha256") ?? "";
  const sha256 = query.get("sha256") ?? "";
  const producer = query.get("producerBuildSha") ?? "";
  const owner = query.get("leaseOwner") ?? "";
  const target = query.get("targetDate") ?? "";
  const attemptText = query.get("attemptCount") ?? "";
  const attempt = Number(attemptText);
  const recoveryId = query.get("recoveryId");
  const planSha256 = query.get("planSha256");
  const scopeSha256 = query.get("scopeSha256");
  const recovery = [recoveryId, planSha256, scopeSha256].some(value => value !== null);
  if (recovery && (!/^[A-Za-z0-9:_-]{1,120}$/.test(recoveryId ?? "")
      || !HASH.test(planSha256 ?? "") || !HASH.test(scopeSha256 ?? ""))) {
    return response({ error: "invalid_checkpoint_recovery_identity" }, 422);
  }
  if (recovery && !["1", "true"].includes(runtime.PIPELINE_RECOVERY_ENABLED ?? "")) {
    return response({ error: "pipeline_recovery_disabled" }, 404);
  }
  if (recovery && !["core_collection", "publish"].includes(stage)) {
    return response({ error: "checkpoint_recovery_stage_unproven" }, 422);
  }
  const maximum = stage === "handoff" ? 16384 : CHECKPOINT_OBJECT_MAX_BYTES;
  if (!parsePipelineRunKey(runKey) || !STAGES.has(stage) || !HASH.test(input) || !HASH.test(sha256) || !SHA.test(producer)
      || !/^[A-Za-z0-9_.:/-]{1,128}$/.test(owner) || !/^[1-9][0-9]{0,8}$/.test(attemptText)
      || !/^\d{4}-\d{2}-\d{2}$/.test(target) || !Number.isSafeInteger(attempt)) {
    return response({ error: "invalid_checkpoint_reference" }, 422);
  }
  const db = runtime.DB.withSession?.("first-primary") ?? runtime.DB;
  const bucket = runtime.RAW_BUCKET;
  let planDeadline: number | undefined;
  let planReads = 0;
  const planKey = recovery ? recoveryPlanObjectKey(recoveryId!, scopeSha256!) : undefined;
  const confirm = async () => {
    const cutoff = new Date(now().getTime() + 30_000).toISOString();
    const row = recovery
      ? await db.prepare(`SELECT p.* FROM pipeline_window p WHERE p.run_key=?
          AND p.lease_owner=? AND p.attempt_count=? AND p.target_date=? AND p.recovery_id=?
          AND p.status IN ('DISPATCHED','RUNNING') AND julianday(p.lease_expires_at)>julianday(?)
          AND EXISTS (SELECT 1 FROM pipeline_window_recovery r WHERE r.recovery_id=p.recovery_id
            AND r.run_key=p.run_key AND r.plan_sha256=? AND r.scope_sha256=? AND r.plan_key=?
            AND r.status='ACTIVE' AND julianday(r.expires_at)>julianday(?)) LIMIT 1`)
        .bind(runKey, owner, attempt, target, recoveryId, cutoff, planSha256, scopeSha256, planKey, cutoff)
        .first<NonNullable<Awaited<ReturnType<typeof getPipelineWindow>>>>()
      : await getPipelineWindow(db, runKey); // Indexed primary-key lookup, LIMIT 1.
    const expiry = Date.parse(row?.lease_expires_at ?? "");
    if (!row || row.lease_owner !== owner || row.attempt_count !== attempt
        || (recovery ? row.recovery_id !== recoveryId : row.recovery_id != null)
        || !["DISPATCHED", "RUNNING"].includes(row.status) || row.target_date !== target
        || !Number.isFinite(expiry) || expiry <= now().getTime() + 30_000) {
      throw new PipelineWindowError("checkpoint_lease_conflict", 409);
    }
    if (recovery) {
      if (planDeadline !== undefined && planDeadline <= Date.parse(cutoff)) {
        throw new PipelineWindowError("checkpoint_recovery_approval_conflict", 409);
      }
    }
  };
  try {
    await confirm();
    if (recovery) {
      if (!("head" in bucket) || typeof bucket.head !== "function") throw new PipelineWindowError("checkpoint_recovery_plan_unavailable", 503);
      // Existing bounded exact-key reader verifies bytes, schema and semantic hash.
      const plan = await readRecoveryPlanObject(bucket as unknown as PlanBucket, planKey!, {
        recoveryId: recoveryId!, runKey, planSha256: planSha256!, scopeSha256: scopeSha256!,
      });
      planReads = 2; // HEAD + GET, not a physical D1 measurement.
      planDeadline = Date.parse(String(plan.deadline));
      if (!(plan.dataDates as string[]).includes(target) || !(plan.requiredStages as string[]).includes(stage)
          || !Number.isFinite(planDeadline) || planDeadline <= now().getTime() + 30_000) {
        throw new PipelineWindowError("checkpoint_recovery_scope_conflict", 409);
      }
      await confirm();
    }
    const runHash = await hash(new TextEncoder().encode(runKey));
    const namespace = recovery ? `recovery/${recoveryId}/${scopeSha256}/` : "";
    const key = `market-data/pipeline/checkpoints/v1/${namespace}${runHash}/${producer}/${stage}/${input}/${sha256}.json`;
    let classA = 0;
    if (request.method === "POST") {
      const length = Number(request.headers.get("content-length"));
      if (length > maximum) return response({ error: "checkpoint_object_too_large" }, 413);
      const bytes = await boundedBytes(request.body, maximum);
      if (await hash(bytes) !== sha256) return response({ error: "checkpoint_digest_mismatch" }, 422);
      await confirm();
      classA = 1;
      await bucket.put(key, bytes, {
        onlyIf: new Headers({ "If-None-Match": "*" }), sha256,
        customMetadata: { sha256, runKey, targetDate: target, producerBuildSha: producer, stage, inputSha256: input,
          ...(recovery ? { recoveryId: recoveryId!, planSha256: planSha256!, scopeSha256: scopeSha256! } : {}) },
      }); // A failed condition returns null: readback must prove exact existing bytes.
    }
    const object = await bucket.get(key);
    if (!object) return response({ error: "checkpoint_not_found" }, 404);
    if (object.size <= 0 || object.size > maximum || object.customMetadata?.sha256 !== sha256
        || object.customMetadata?.runKey !== runKey || object.customMetadata?.targetDate !== target
        || object.customMetadata?.producerBuildSha !== producer || object.customMetadata?.stage !== stage
        || object.customMetadata?.inputSha256 !== input
        || (recovery && (object.customMetadata?.recoveryId !== recoveryId || object.customMetadata?.planSha256 !== planSha256
          || object.customMetadata?.scopeSha256 !== scopeSha256))) {
      return response({ error: "checkpoint_object_conflict" }, 409);
    }
    const bytes = await boundedBytes(object.body, maximum);
    if (bytes.byteLength !== object.size || await hash(bytes) !== sha256) return response({ error: "checkpoint_object_conflict" }, 409);
    await confirm(); // Immutable orphan writes never imply completion after lease loss.
    const usage = { d1RowsRead: null, d1RowsWritten: 0, r2ClassA: classA, r2ClassB: 1 + planReads, r2Bytes: bytes.byteLength };
    if (request.method === "POST") return response({ decision: "verified", key, sha256, bytes: bytes.byteLength, usage });
    return new Response(bytes, { headers: { "Content-Type": "application/json", "Cache-Control": "private, no-store",
      "X-Flow-Checkpoint-Sha256": sha256, "X-Flow-Checkpoint-Key": key, "X-Flow-Usage": JSON.stringify(usage) } });
  } catch (error) {
    if (error instanceof PipelineWindowError) return response({ error: error.message }, error.status);
    return response({ error: "checkpoint_transport_unavailable" }, 503);
  }
}
