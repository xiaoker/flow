/** Optional ordinary-run authority. Legacy uploads do not use this contract. */
import { getPipelineWindow, parsePipelineRunKey, PipelineWindowError } from "./pipeline-window.ts";

type DB = Parameters<typeof getPipelineWindow>[0];
export type CandidateExecution = {
  runKey: string; leaseOwner: string; attemptCount: number;
  targetDate: string; producerBuildSha: string;
};

export function parseCandidateExecution(value: unknown, manifest: {
  schemaVersion: number; asOf: string; producerBuildSha?: string;
}, activationMode: unknown): CandidateExecution {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PipelineWindowError("invalid_candidate_execution", 422);
  }
  const ref = value as Record<string, unknown>;
  const keys = ["runKey", "leaseOwner", "attemptCount", "targetDate", "producerBuildSha"];
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

export function candidateUploadAuthority(db: DB, ref: CandidateExecution, now = () => new Date()) {
  const confirm = async () => {
    const row = await getPipelineWindow(db, ref.runKey);
    const expiry = Date.parse(row?.lease_expires_at ?? "");
    if (!row || row.lease_owner !== ref.leaseOwner || row.attempt_count !== ref.attemptCount
        || row.target_date !== ref.targetDate || row.recovery_id != null
        || !["DISPATCHED", "RUNNING"].includes(row.status)
        || !Number.isFinite(expiry) || expiry <= now().getTime() + 30000) {
      throw new PipelineWindowError("candidate_lease_conflict", 409);
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
      const stamp = now();
      // Authority is checked in the same D1 statement as the insertion. A prior
      // SELECT alone cannot fence takeover. Existing release identities are immutable.
      await db.prepare(`INSERT INTO release_manifest
        (release_id, as_of, schema_version, manifest_sha256, manifest_json, observed_at, status, created_at)
        SELECT ?, ?, ?, ?, ?, ?, 'STAGED', ?
        WHERE EXISTS (SELECT 1 FROM pipeline_window WHERE run_key = ? AND lease_owner = ?
          AND attempt_count = ? AND target_date = ? AND recovery_id IS NULL
          AND status IN ('DISPATCHED', 'RUNNING') AND julianday(lease_expires_at) > julianday(?))
        ON CONFLICT(release_id) DO NOTHING`)
        .bind(release.releaseId, release.asOf, manifest.schemaVersion, release.manifestSha256,
          manifestJson, manifest.observedAt ?? null, stamp.toISOString(), ref.runKey,
          ref.leaseOwner, ref.attemptCount, ref.targetDate, new Date(stamp.getTime() + 30000).toISOString()).run();
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
