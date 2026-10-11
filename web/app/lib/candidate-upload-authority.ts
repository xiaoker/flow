/** Optional ordinary-run authority. Legacy uploads do not use this contract. */
import { getPipelineWindow, parsePipelineRunKey, PipelineWindowError } from "./pipeline-window.ts";
import { readRecoveryPlanObject, recoveryPlanObjectKey } from "./pipeline-recovery-plan.ts";

type DB = Parameters<typeof getPipelineWindow>[0] & { withSession?(constraint: string): DB };
export type CandidateExecution = {
  runKey: string; leaseOwner: string; attemptCount: number;
  targetDate: string; producerBuildSha: string;
  recoveryId?: string; planSha256?: string; scopeSha256?: string;
};

export function parseCandidateExecution(value: unknown, manifest: {
  schemaVersion: number; asOf: string; producerBuildSha?: string;
}, activationMode: unknown): CandidateExecution {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PipelineWindowError("invalid_candidate_execution", 422);
  }
  const ref = value as Record<string, unknown>;
  const keys = ["runKey", "leaseOwner", "attemptCount", "targetDate", "producerBuildSha"];
  const recovery = ["recoveryId", "planSha256", "scopeSha256"].some(key => Object.hasOwn(ref, key));
  if (recovery) {
    keys.push("recoveryId", "planSha256", "scopeSha256");
    if (typeof ref.recoveryId !== "string" || !/^[A-Za-z0-9:_-]{1,120}$/.test(ref.recoveryId)
        || typeof ref.planSha256 !== "string" || !/^[0-9a-f]{64}$/.test(ref.planSha256)
        || typeof ref.scopeSha256 !== "string" || !/^[0-9a-f]{64}$/.test(ref.scopeSha256)) {
      throw new PipelineWindowError("invalid_candidate_execution", 422);
    }
  }
  if (Object.keys(ref).length !== keys.length || !keys.every((key) => Object.hasOwn(ref, key))
      || typeof ref.runKey !== "string" || !parsePipelineRunKey(ref.runKey)
      || typeof ref.leaseOwner !== "string" || !/^[A-Za-z0-9_.:/-]{1,128}$/.test(ref.leaseOwner)
      || typeof ref.attemptCount !== "number" || !Number.isSafeInteger(ref.attemptCount)
      || ref.attemptCount < 1 || ref.attemptCount > 999999999
      || typeof ref.targetDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(ref.targetDate)
      || !Number.isFinite(Date.parse(`${ref.targetDate}T00:00:00Z`))
      || new Date(`${ref.targetDate}T00:00:00Z`).toISOString().slice(0, 10) !== ref.targetDate
      || typeof ref.producerBuildSha !== "string" || !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(ref.producerBuildSha)) {
    throw new PipelineWindowError("invalid_candidate_execution", 422);
  }
  if (activationMode !== "stage_only" || manifest.schemaVersion !== 4) {
    throw new PipelineWindowError("runner_candidate_requires_stage_only_v4", 409);
  }
  if (ref.targetDate !== manifest.asOf || ref.producerBuildSha !== manifest.producerBuildSha) {
    throw new PipelineWindowError("candidate_execution_manifest_mismatch", 409);
  }
  return ref as CandidateExecution;
}

export function candidateUploadAuthority(db: DB, ref: CandidateExecution, now = () => new Date(),
  recoveryRuntime?: { bucket: Parameters<typeof readRecoveryPlanObject>[0]; enabled?: string }) {
  const recovery = ref.recoveryId !== undefined;
  if (recovery && !["1", "true"].includes(recoveryRuntime?.enabled ?? "")) {
    throw new PipelineWindowError("pipeline_recovery_disabled", 404);
  }
  if (recovery) db = db.withSession?.("first-primary") ?? db;
  const recoveryPredicate = `recovery_id = ? AND EXISTS
    (SELECT 1 FROM pipeline_window_recovery r WHERE r.recovery_id = pipeline_window.recovery_id
      AND r.run_key = pipeline_window.run_key AND r.plan_sha256 = ? AND r.scope_sha256 = ?
      AND r.plan_key = ? AND r.status = 'ACTIVE' AND julianday(r.expires_at) > julianday(?))`;
  const recoveryArgs = (cutoff: string) => [ref.recoveryId, ref.planSha256, ref.scopeSha256,
    recoveryPlanObjectKey(ref.recoveryId!, ref.scopeSha256!), cutoff];
  let deadline: number | undefined;
  let planLoaded = false;
  const confirm = async () => {
    const cutoff = new Date(now().getTime() + 30000).toISOString();
    const row = recovery ? await db.prepare(`SELECT * FROM pipeline_window WHERE run_key = ?
      AND lease_owner = ? AND attempt_count = ? AND target_date = ?
      AND status IN ('DISPATCHED', 'RUNNING') AND julianday(lease_expires_at) > julianday(?)
      AND ${recoveryPredicate} LIMIT 1`).bind(ref.runKey, ref.leaseOwner, ref.attemptCount,
        ref.targetDate, cutoff, ...recoveryArgs(cutoff))
      .first<NonNullable<Awaited<ReturnType<typeof getPipelineWindow>>>>()
      : await getPipelineWindow(db, ref.runKey);
    const expiry = Date.parse(row?.lease_expires_at ?? "");
    if (!row || row.lease_owner !== ref.leaseOwner || row.attempt_count !== ref.attemptCount
        || row.target_date !== ref.targetDate || (recovery ? row.recovery_id !== ref.recoveryId : row.recovery_id != null)
        || !["DISPATCHED", "RUNNING"].includes(row.status)
        || !Number.isFinite(expiry) || expiry <= now().getTime() + 30000) {
      throw new PipelineWindowError("candidate_lease_conflict", 409);
    }
    if (recovery && !planLoaded) {
      const plan = await readRecoveryPlanObject(recoveryRuntime!.bucket,
        recoveryPlanObjectKey(ref.recoveryId!, ref.scopeSha256!), {
          recoveryId: ref.recoveryId!, runKey: ref.runKey, planSha256: ref.planSha256!, scopeSha256: ref.scopeSha256!,
        });
      deadline = Date.parse(String(plan.deadline));
      if (!(plan.dataDates as string[]).includes(ref.targetDate)
          || !(plan.requiredStages as string[]).includes("publish") || !Number.isFinite(deadline)) {
        throw new PipelineWindowError("candidate_recovery_scope_conflict", 409);
      }
      planLoaded = true;
      await confirm(); // Recheck approval after the bounded immutable plan read.
    }
    if (recovery && deadline! <= Date.parse(cutoff)) {
      throw new PipelineWindowError("candidate_recovery_deadline_expired", 409);
    }
  };
  return {
    confirm,
    async effect<T>(operation: () => Promise<T>): Promise<T> {
      await confirm();
      const result = await operation();
      await confirm();
      return result;
    },
    async stage(release: { releaseId: string; asOf: string; manifestSha256: string },
      manifest: { schemaVersion: number; observedAt?: string | null }, manifestJson: string): Promise<"staged_candidate"> {
      await confirm();
      if (release.asOf !== ref.targetDate) throw new PipelineWindowError("candidate_stage_conflict", 409);
      const stamp = now();
      // Authority is checked in the same D1 statement as the insertion. A prior
      // SELECT alone cannot fence takeover. Existing release identities are immutable.
      await db.prepare(`INSERT INTO release_manifest
        (release_id, as_of, schema_version, manifest_sha256, manifest_json, observed_at, status, created_at)
        SELECT ?, ?, ?, ?, ?, ?, 'STAGED', ?
        WHERE EXISTS (SELECT 1 FROM pipeline_window WHERE run_key = ? AND lease_owner = ?
          AND attempt_count = ? AND target_date = ?
          AND status IN ('DISPATCHED', 'RUNNING') AND julianday(lease_expires_at) > julianday(?)
          AND ${recovery ? recoveryPredicate : "recovery_id IS NULL"})
        ON CONFLICT(release_id) DO NOTHING`)
        .bind(release.releaseId, release.asOf, manifest.schemaVersion, release.manifestSha256,
          manifestJson, manifest.observedAt ?? null, stamp.toISOString(), ref.runKey,
          ref.leaseOwner, ref.attemptCount, ref.targetDate, new Date(stamp.getTime() + 30000).toISOString(),
          ...(recovery ? recoveryArgs(new Date(stamp.getTime() + 30000).toISOString()) : [])).run();
      await confirm();
      const saved = await db.prepare(`SELECT as_of, manifest_sha256, manifest_json, status
        FROM release_manifest WHERE release_id = ? LIMIT 1`).bind(release.releaseId)
        .first<{ as_of: string; manifest_sha256: string; manifest_json: string; status: string }>();
      if (!saved || saved.status !== "STAGED" || saved.as_of !== release.asOf
          || saved.manifest_sha256 !== release.manifestSha256 || saved.manifest_json !== manifestJson) {
        throw new PipelineWindowError("candidate_stage_conflict", 409);
      }
      await confirm();
      return "staged_candidate";
    },
  };
}
