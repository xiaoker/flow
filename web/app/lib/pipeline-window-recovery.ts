/**
 * Audited recovery control-plane helpers.
 *
 * A recovery is deliberately a two-step operation: registration records an
 * immutable plan and the exact pre-recovery state, while claim atomically
 * binds that authorization to one pipeline_window lease.  Nothing here reads
 * ETF facts or release payloads and no production flag is enabled by code.
 */

import {
  parsePipelineRunKey,
  PipelineWindowError,
  type PipelineWindowRow,
} from "./pipeline-window.ts";

export { PipelineWindowError };

export const PIPELINE_RECOVERY_STATUSES = [
  "AUTHORIZED",
  "ACTIVE",
  "PAUSED",
  "COMPLETED",
  "BLOCKED",
  "REVOKED",
] as const;

export type PipelineRecoveryStatus = (typeof PIPELINE_RECOVERY_STATUSES)[number];

export type PipelineWindowRecoveryRow = {
  recovery_id: string;
  run_key: string;
  plan_key: string;
  plan_sha256: string;
  scope_sha256: string;
  expected_attempt_count: number;
  expected_updated_at: string;
  previous_state_json: string;
  reason_code: string;
  approval_reference: string;
  authorized_at: string;
  expires_at: string;
  status: PipelineRecoveryStatus | string;
  latest_receipt_key: string | null;
  latest_receipt_sha256: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type PipelineRecoveryRegistrationInput = {
  recoveryId: string;
  runKey: string;
  planKey: string;
  planSha256: string;
  scopeSha256: string;
  expectedAttemptCount: number;
  expectedUpdatedAt: string;
  previousStateJson: string;
  reasonCode: string;
  approvalReference: string;
  authorizedAt: string;
  expiresAt: string;
  /**
   * Optional expired PAUSED authorization that is being replaced.  This is
   * intentionally part of the registration contract so replacing a stale
   * control-plane generation cannot be done with an ad-hoc D1 UPDATE.
   */
  supersedesRecoveryId?: string;
};

export type PipelineRecoveryClaimInput = {
  recoveryId: string;
  runKey: string;
  leaseOwner: string;
  sourceFingerprint?: string | null;
  githubRunId?: string | null;
};

export type PipelineRecoveryClaimDecision =
  | "acquired"
  | "takeover"
  | "already_satisfied"
  | "skip_leased"
  | "skip_blocked";

export type PipelineRecoveryClaim = {
  recoveryId: string;
  runKey: string;
  decision: PipelineRecoveryClaimDecision;
  recoveryStatus: string;
  status: string;
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  attemptCount: number;
  rowsWritten: number;
  rowsRead: number;
  recovery: PipelineWindowRecoveryRow;
  row: PipelineWindowRow;
};

type D1Result = {
  meta?: {
    changes?: unknown;
    rows_read?: unknown;
    rows_written?: unknown;
  };
};

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  run(): Promise<D1Result>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
};

type D1Like = {
  prepare(sql: string): D1Statement;
  batch?: (statements: D1Statement[]) => Promise<D1Result[]>;
};

export const PIPELINE_RECOVERY_LEASE_MS = 15 * 60 * 1_000;

const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9:_./-]{1,200}$/;
const OWNER = /^[A-Za-z0-9:_-]{8,120}$/;
const ERROR_CODE = /^[A-Za-z0-9._:-]{1,160}$/;
const GITHUB_RUN_ID = /^\d{1,30}$/;
const MAX_PREVIOUS_STATE_BYTES = 200_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function validInstant(value: string): boolean {
  return ISO_INSTANT.test(value) && !Number.isNaN(Date.parse(value));
}

function requireSafeNonNegativeInteger(value: unknown, errorCode: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new PipelineWindowError(errorCode, 422);
  }
  return Number(value);
}

function requireMetaInteger(value: unknown, errorCode: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new PipelineWindowError(errorCode, 503);
  }
  return Number(value);
}

function requireChanges(result: D1Result, errorCode = "pipeline_recovery_change_meta_invalid"): number {
  return requireMetaInteger(result.meta?.changes, errorCode);
}

function rowsWritten(result: D1Result): number {
  return requireMetaInteger(result.meta?.rows_written, "pipeline_recovery_write_meta_invalid");
}

function rowsRead(result: D1Result): number {
  // Local SQLite test doubles do not expose D1 physical-read metadata.  Keep
  // that adapter compatible, while production D1 responses contribute their
  // measured rows_read value to the recovery receipt.
  if (result.meta?.rows_read === undefined) return 0;
  return requireMetaInteger(result.meta.rows_read, "pipeline_recovery_read_meta_invalid");
}

function sumRowsRead(results: D1Result[]): number {
  const total = results.reduce((sum, result) => sum + rowsRead(result), 0);
  if (total > 10_000) throw new PipelineWindowError("pipeline_recovery_read_budget_exceeded", 503);
  return total;
}

function sumRowsWritten(results: D1Result[]): number {
  const total = results.reduce((sum, result) => sum + rowsWritten(result), 0);
  // A recovery claim is only two control-plane rows.  This keeps malformed
  // adapters from hiding an accidental broad write while still allowing D1
  // to count index writes physically.
  if (total > 32) throw new PipelineWindowError("pipeline_recovery_write_budget_exceeded", 503);
  return total;
}

function requireBatch(db: D1Like): (statements: D1Statement[]) => Promise<D1Result[]> {
  if (typeof db.batch !== "function") {
    throw new PipelineWindowError("pipeline_recovery_transaction_unavailable", 503);
  }
  return db.batch.bind(db);
}

function immutableRegistrationFields(input: PipelineRecoveryRegistrationInput): string[] {
  return [
    input.runKey,
    input.planKey,
    input.planSha256,
    input.scopeSha256,
    String(input.expectedAttemptCount),
    input.expectedUpdatedAt,
    input.previousStateJson,
    input.reasonCode,
    input.approvalReference,
    input.authorizedAt,
    input.expiresAt,
  ];
}

function sameRegistration(
  row: PipelineWindowRecoveryRow,
  input: PipelineRecoveryRegistrationInput,
): boolean {
  const expected = immutableRegistrationFields(input);
  return [
    row.run_key,
    row.plan_key,
    row.plan_sha256,
    row.scope_sha256,
    String(row.expected_attempt_count),
    row.expected_updated_at,
    row.previous_state_json,
    row.reason_code,
    row.approval_reference,
    row.authorized_at,
    row.expires_at,
  ].every((value, index) => value === expected[index]);
}

function selectRecoverySql(): string {
  return `SELECT recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
                 expected_attempt_count, expected_updated_at, previous_state_json,
                 reason_code, approval_reference, authorized_at, expires_at,
                 status, latest_receipt_key, latest_receipt_sha256,
                 created_at, updated_at, completed_at
            FROM pipeline_window_recovery`;
}

function selectPipelineSql(): string {
  return `SELECT run_key, pipeline, window_date, window_name, contract_version,
                 target_date, status, lease_owner, lease_expires_at,
                 source_fingerprint, github_run_id, attempt_count, last_error_code,
                 created_at, updated_at, completed_at, recovery_id
            FROM pipeline_window`;
}

function serializePipelineRow(value: Record<string, unknown>): PipelineWindowRow {
  return {
    run_key: String(value.run_key),
    pipeline: String(value.pipeline),
    window_date: String(value.window_date),
    window_name: String(value.window_name),
    contract_version: String(value.contract_version),
    target_date: value.target_date === null || value.target_date === undefined ? null : String(value.target_date),
    status: String(value.status),
    lease_owner: value.lease_owner === null || value.lease_owner === undefined ? null : String(value.lease_owner),
    lease_expires_at: value.lease_expires_at === null || value.lease_expires_at === undefined ? null : String(value.lease_expires_at),
    source_fingerprint: value.source_fingerprint === null || value.source_fingerprint === undefined ? null : String(value.source_fingerprint),
    github_run_id: value.github_run_id === null || value.github_run_id === undefined ? null : String(value.github_run_id),
    attempt_count: Number(value.attempt_count ?? 0),
    last_error_code: value.last_error_code === null || value.last_error_code === undefined ? null : String(value.last_error_code),
    created_at: String(value.created_at),
    updated_at: String(value.updated_at),
    completed_at: value.completed_at === null || value.completed_at === undefined ? null : String(value.completed_at),
    recovery_id: value.recovery_id === null || value.recovery_id === undefined ? null : String(value.recovery_id),
  };
}

function serializeRecoveryRow(value: Record<string, unknown>): PipelineWindowRecoveryRow {
  return {
    recovery_id: String(value.recovery_id),
    run_key: String(value.run_key),
    plan_key: String(value.plan_key),
    plan_sha256: String(value.plan_sha256),
    scope_sha256: String(value.scope_sha256),
    expected_attempt_count: Number(value.expected_attempt_count ?? 0),
    expected_updated_at: String(value.expected_updated_at),
    previous_state_json: String(value.previous_state_json),
    reason_code: String(value.reason_code),
    approval_reference: String(value.approval_reference),
    authorized_at: String(value.authorized_at),
    expires_at: String(value.expires_at),
    status: String(value.status),
    latest_receipt_key: value.latest_receipt_key === null || value.latest_receipt_key === undefined ? null : String(value.latest_receipt_key),
    latest_receipt_sha256: value.latest_receipt_sha256 === null || value.latest_receipt_sha256 === undefined ? null : String(value.latest_receipt_sha256),
    created_at: String(value.created_at),
    updated_at: String(value.updated_at),
    completed_at: value.completed_at === null || value.completed_at === undefined ? null : String(value.completed_at),
  };
}

export function validatePipelineRecoveryRegistrationInput(input: PipelineRecoveryRegistrationInput): void {
  const parsed = parsePipelineRunKey(input.runKey);
  if (!parsed) throw new PipelineWindowError("invalid_run_key", 422);
  if (!IDENTIFIER.test(input.recoveryId)) throw new PipelineWindowError("invalid_recovery_id", 422);
  if (!IDENTIFIER.test(input.planKey)) throw new PipelineWindowError("invalid_plan_key", 422);
  if (!SHA256.test(input.planSha256)) throw new PipelineWindowError("invalid_plan_sha256", 422);
  if (!SHA256.test(input.scopeSha256)) throw new PipelineWindowError("invalid_scope_sha256", 422);
  requireSafeNonNegativeInteger(input.expectedAttemptCount, "invalid_expected_attempt_count");
  if (!validInstant(input.expectedUpdatedAt)) throw new PipelineWindowError("invalid_expected_updated_at", 422);
  if (typeof input.previousStateJson !== "string" || new TextEncoder().encode(input.previousStateJson).byteLength > MAX_PREVIOUS_STATE_BYTES) {
    throw new PipelineWindowError("invalid_previous_state_json", 422);
  }
  try {
    if (!isRecord(JSON.parse(input.previousStateJson))) throw new Error("object_required");
  } catch {
    throw new PipelineWindowError("invalid_previous_state_json", 422);
  }
  if (!ERROR_CODE.test(input.reasonCode)) throw new PipelineWindowError("invalid_recovery_reason", 422);
  if (!IDENTIFIER.test(input.approvalReference)) throw new PipelineWindowError("invalid_approval_reference", 422);
  if (!validInstant(input.authorizedAt)) throw new PipelineWindowError("invalid_authorized_at", 422);
  if (!validInstant(input.expiresAt) || Date.parse(input.expiresAt) <= Date.parse(input.authorizedAt)) {
    throw new PipelineWindowError("invalid_recovery_expiry", 422);
  }
  if (
    input.supersedesRecoveryId !== undefined &&
    (typeof input.supersedesRecoveryId !== "string" || !IDENTIFIER.test(input.supersedesRecoveryId))
  ) {
    throw new PipelineWindowError("invalid_supersedes_recovery_id", 422);
  }
}

function validateClaim(input: PipelineRecoveryClaimInput): void {
  if (!IDENTIFIER.test(input.recoveryId)) throw new PipelineWindowError("invalid_recovery_id", 422);
  if (!parsePipelineRunKey(input.runKey)) throw new PipelineWindowError("invalid_run_key", 422);
  if (!OWNER.test(input.leaseOwner)) throw new PipelineWindowError("invalid_lease_owner", 422);
  if (input.sourceFingerprint !== undefined && input.sourceFingerprint !== null && !IDENTIFIER.test(input.sourceFingerprint)) {
    throw new PipelineWindowError("invalid_source_fingerprint", 422);
  }
  if (input.githubRunId !== undefined && input.githubRunId !== null && !GITHUB_RUN_ID.test(input.githubRunId)) {
    throw new PipelineWindowError("invalid_github_run_id", 422);
  }
}

function activeLease(row: PipelineWindowRow, nowIso: string): boolean {
  return (
    (row.status === "DISPATCHED" || row.status === "RUNNING") &&
    typeof row.lease_expires_at === "string" &&
    row.lease_expires_at > nowIso
  );
}

function leaseExpired(row: PipelineWindowRow, nowIso: string): boolean {
  return row.lease_expires_at === null || row.lease_expires_at <= nowIso;
}

type AtomicPairResult = {
  changes: [number, number];
  rowsWritten: number;
  rowsRead: number;
};

type AtomicRecoverySupersedeResult = {
  changes: [number, number, number];
  rowsWritten: number;
  rowsRead: number;
};

/**
 * Run a two-row recovery mutation as one D1 batch with transaction guards.
 * D1 treats a conditional UPDATE that matches zero rows as a successful
 * statement, so a plain two-statement batch could otherwise commit one side
 * of a CAS and leave the audit row and lease out of sync. Each guard attempts
 * a duplicate primary-key insert when its preceding UPDATE did not change
 * exactly one row; the guard emits either a duplicate key (when the target
 * exists) or a NOT NULL sentinel (when it does not), so SQLite aborts the
 * batch and rolls it back. The guard constraint error is returned as `null`,
 * allowing callers to classify a normal concurrent loser after re-reading
 * the rows.
 */
async function runAtomicPair(
  db: D1Like,
  pipelineRunKey: string,
  recoveryId: string,
  pipelineMutation: D1Statement,
  recoveryMutation: D1Statement,
): Promise<AtomicPairResult | null> {
  const batch = requireBatch(db);
  const pipelineGuard = db.prepare(
    `INSERT INTO pipeline_window (
       run_key, pipeline, window_date, window_name, contract_version, status,
       created_at, updated_at
     )
     SELECT run_key, pipeline, window_date, window_name, contract_version,
            status, created_at, updated_at
       FROM pipeline_window
      WHERE run_key = ? AND changes() <> 1
     UNION ALL
     SELECT NULL, '', '', '', '', '', '', ''
       WHERE changes() <> 1
         AND NOT EXISTS (SELECT 1 FROM pipeline_window WHERE run_key = ?)`,
  ).bind(pipelineRunKey, pipelineRunKey);
  const recoveryGuard = db.prepare(
    `INSERT INTO pipeline_window_recovery (
       recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
       expected_attempt_count, expected_updated_at, previous_state_json,
       reason_code, approval_reference, authorized_at, expires_at, status,
       created_at, updated_at
     )
     SELECT recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
            expected_attempt_count, expected_updated_at, previous_state_json,
            reason_code, approval_reference, authorized_at, expires_at, status,
            created_at, updated_at
       FROM pipeline_window_recovery
      WHERE recovery_id = ? AND changes() <> 1
     UNION ALL
     SELECT NULL, '', '', '', '', 0, '', '', '', '', '', '', '', '', ''
       WHERE changes() <> 1
         AND NOT EXISTS (SELECT 1 FROM pipeline_window_recovery WHERE recovery_id = ?)`,
  ).bind(recoveryId, recoveryId);
  try {
    const results = await batch([pipelineMutation, pipelineGuard, recoveryMutation, recoveryGuard]);
    const pipelineChanges = requireChanges(results[0]);
    const recoveryChanges = requireChanges(results[2]);
    if (pipelineChanges !== 1 || recoveryChanges !== 1) {
      throw new PipelineWindowError("pipeline_recovery_transaction_invariant", 503);
    }
    return {
      changes: [pipelineChanges, recoveryChanges],
      rowsWritten: sumRowsWritten(results),
      rowsRead: sumRowsRead(results),
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    if (/(?:UNIQUE|NOT NULL) constraint failed:\s*(?:pipeline_window|pipeline_window_recovery)\./i.test(message)) {
      return null;
    }
    throw error;
  }
}

/**
 * Atomically retire one expired PAUSED recovery generation, unbind its
 * pipeline row, and insert the replacement authorization.  The partial
 * unique index on active/paused generations means this must be one SQLite
 * transaction; separate UPDATE/INSERT calls can leave a stale generation
 * permanently blocking the next plan.
 */
async function runAtomicRecoverySupersede(
  db: D1Like,
  runKey: string,
  oldRecoveryId: string,
  newInput: PipelineRecoveryRegistrationInput,
  createdAt: string,
  nowIso: string,
): Promise<AtomicRecoverySupersedeResult | null> {
  const batch = requireBatch(db);
  const pipelineMutation = db.prepare(
    `UPDATE pipeline_window
        SET recovery_id = NULL
      WHERE run_key = ? AND recovery_id = ? AND status = 'FAILED_RETRYABLE'
        AND lease_owner IS NULL
        AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
        AND attempt_count = ? AND updated_at = ?`,
  ).bind(
    runKey,
    oldRecoveryId,
    nowIso,
    newInput.expectedAttemptCount,
    newInput.expectedUpdatedAt,
  );
  const pipelineGuard = db.prepare(
    `INSERT INTO pipeline_window (
       run_key, pipeline, window_date, window_name, contract_version, status,
       created_at, updated_at
     )
     SELECT run_key, pipeline, window_date, window_name, contract_version,
            status, created_at, updated_at
       FROM pipeline_window
      WHERE run_key = ? AND changes() <> 1
     UNION ALL
     SELECT NULL, '', '', '', '', '', '', ''
       WHERE changes() <> 1
         AND NOT EXISTS (SELECT 1 FROM pipeline_window WHERE run_key = ?)`,
  ).bind(runKey, runKey);
  const oldRecoveryMutation = db.prepare(
    `UPDATE pipeline_window_recovery
        SET status = 'REVOKED', updated_at = ?
      WHERE recovery_id = ? AND run_key = ? AND status = 'PAUSED'
        AND expires_at <= ?`,
  ).bind(nowIso, oldRecoveryId, runKey, nowIso);
  const oldRecoveryGuard = db.prepare(
    `INSERT INTO pipeline_window_recovery (
       recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
       expected_attempt_count, expected_updated_at, previous_state_json,
       reason_code, approval_reference, authorized_at, expires_at, status,
       created_at, updated_at
     )
     SELECT recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
            expected_attempt_count, expected_updated_at, previous_state_json,
            reason_code, approval_reference, authorized_at, expires_at, status,
            created_at, updated_at
       FROM pipeline_window_recovery
      WHERE recovery_id = ? AND changes() <> 1
     UNION ALL
     SELECT NULL, '', '', '', '', 0, '', '', '', '', '', '', '', '', ''
       WHERE changes() <> 1
         AND NOT EXISTS (SELECT 1 FROM pipeline_window_recovery WHERE recovery_id = ?)`,
  ).bind(oldRecoveryId, oldRecoveryId);
  const newRecoveryInsert = db.prepare(
    `INSERT INTO pipeline_window_recovery (
       recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
       expected_attempt_count, expected_updated_at, previous_state_json,
       reason_code, approval_reference, authorized_at, expires_at, status,
       latest_receipt_key, latest_receipt_sha256, created_at, updated_at, completed_at
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTHORIZED', NULL, NULL, ?, ?, NULL)`,
  ).bind(
    newInput.recoveryId,
    newInput.runKey,
    newInput.planKey,
    newInput.planSha256,
    newInput.scopeSha256,
    newInput.expectedAttemptCount,
    newInput.expectedUpdatedAt,
    newInput.previousStateJson,
    newInput.reasonCode,
    newInput.approvalReference,
    newInput.authorizedAt,
    newInput.expiresAt,
    createdAt,
    createdAt,
  );
  const newRecoveryGuard = db.prepare(
    `INSERT INTO pipeline_window_recovery (
       recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
       expected_attempt_count, expected_updated_at, previous_state_json,
       reason_code, approval_reference, authorized_at, expires_at, status,
       created_at, updated_at
     )
     SELECT recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
            expected_attempt_count, expected_updated_at, previous_state_json,
            reason_code, approval_reference, authorized_at, expires_at, status,
            created_at, updated_at
       FROM pipeline_window_recovery
      WHERE recovery_id = ? AND changes() <> 1
     UNION ALL
     SELECT NULL, '', '', '', '', 0, '', '', '', '', '', '', '', '', ''
       WHERE changes() <> 1
         AND NOT EXISTS (SELECT 1 FROM pipeline_window_recovery WHERE recovery_id = ?)`,
  ).bind(newInput.recoveryId, newInput.recoveryId);

  try {
    const results = await batch([
      pipelineMutation,
      pipelineGuard,
      oldRecoveryMutation,
      oldRecoveryGuard,
      newRecoveryInsert,
      newRecoveryGuard,
    ]);
    const pipelineChanges = requireChanges(results[0]);
    const oldRecoveryChanges = requireChanges(results[2]);
    const newRecoveryChanges = requireChanges(results[4]);
    if (pipelineChanges !== 1 || oldRecoveryChanges !== 1 || newRecoveryChanges !== 1) {
      throw new PipelineWindowError("pipeline_recovery_supersede_invariant", 503);
    }
    return {
      changes: [pipelineChanges, oldRecoveryChanges, newRecoveryChanges],
      rowsWritten: sumRowsWritten(results),
      rowsRead: sumRowsRead(results),
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    if (/(?:UNIQUE|NOT NULL) constraint failed:\s*(?:pipeline_window|pipeline_window_recovery)\./i.test(message)) {
      return null;
    }
    throw error;
  }
}

async function readPipelineWindow(db: D1Like, runKey: string): Promise<PipelineWindowRow | null> {
  const row = await db.prepare(`${selectPipelineSql()} WHERE run_key = ? LIMIT 1`).bind(runKey).first<Record<string, unknown>>();
  return row ? serializePipelineRow(row) : null;
}

export async function getPipelineWindowRecovery(
  db: D1Like,
  recoveryId: string,
): Promise<PipelineWindowRecoveryRow | null> {
  if (!IDENTIFIER.test(recoveryId)) throw new PipelineWindowError("invalid_recovery_id", 422);
  const row = await db.prepare(`${selectRecoverySql()} WHERE recovery_id = ? LIMIT 1`).bind(recoveryId).first<Record<string, unknown>>();
  return row ? serializeRecoveryRow(row) : null;
}

export type PipelineRecoveryRegistration = {
  decision: "registered" | "already_registered" | "superseded";
  recovery: PipelineWindowRecoveryRow;
  pipeline: PipelineWindowRow;
  supersededRecoveryId?: string;
  rowsWritten: number;
  rowsRead: number;
};

/** Register an authorization, optionally replacing one expired generation atomically. */
export async function registerPipelineWindowRecovery(
  db: D1Like,
  input: PipelineRecoveryRegistrationInput,
  now = new Date(),
): Promise<PipelineRecoveryRegistration> {
  validatePipelineRecoveryRegistrationInput(input);
  const pipeline = await readPipelineWindow(db, input.runKey);
  if (!pipeline) throw new PipelineWindowError("pipeline_window_not_found", 404);

  const existingById = await getPipelineWindowRecovery(db, input.recoveryId);
  if (existingById) {
    if (!sameRegistration(existingById, input)) throw new PipelineWindowError("recovery_registration_conflict", 409);
    return { decision: "already_registered", recovery: existingById, pipeline, rowsWritten: 0, rowsRead: 0 };
  }
  const existingByPlan = await db.prepare(
    `${selectRecoverySql()} WHERE run_key = ? AND plan_sha256 = ? LIMIT 1`,
  ).bind(input.runKey, input.planSha256).first<Record<string, unknown>>();
  if (existingByPlan) {
    const existing = serializeRecoveryRow(existingByPlan);
    if (existing.recovery_id !== input.recoveryId) {
      throw new PipelineWindowError("recovery_scope_conflict", 409);
    }
    if (!sameRegistration(existing, input)) throw new PipelineWindowError("recovery_scope_conflict", 409);
    return { decision: "already_registered", recovery: existing, pipeline, rowsWritten: 0, rowsRead: 0 };
  }

  const createdAt = now.toISOString();
  if (input.supersedesRecoveryId !== undefined) {
    if (input.supersedesRecoveryId === input.recoveryId) {
      throw new PipelineWindowError("recovery_supersede_self_conflict", 409);
    }
    const oldRecovery = await getPipelineWindowRecovery(db, input.supersedesRecoveryId);
    if (!oldRecovery) throw new PipelineWindowError("superseded_recovery_not_found", 404);
    if (oldRecovery.run_key !== input.runKey) {
      throw new PipelineWindowError("superseded_recovery_run_key_mismatch", 409);
    }
    if (oldRecovery.status !== "PAUSED" || Date.parse(oldRecovery.expires_at) > now.getTime()) {
      throw new PipelineWindowError("superseded_recovery_not_expired", 409);
    }
    if (
      pipeline.recovery_id !== oldRecovery.recovery_id ||
      pipeline.status !== "FAILED_RETRYABLE" ||
      pipeline.lease_owner !== null ||
      !leaseExpired(pipeline, createdAt) ||
      pipeline.attempt_count !== input.expectedAttemptCount ||
      pipeline.updated_at !== input.expectedUpdatedAt
    ) {
      throw new PipelineWindowError("recovery_supersede_state_conflict", 409);
    }
    const transaction = await runAtomicRecoverySupersede(
      db,
      input.runKey,
      oldRecovery.recovery_id,
      input,
      createdAt,
      createdAt,
    );
    if (!transaction) {
      const concurrent = await getPipelineWindowRecovery(db, input.recoveryId);
      if (concurrent && sameRegistration(concurrent, input)) {
        const nextPipeline = await readPipelineWindow(db, input.runKey);
        if (!nextPipeline) throw new PipelineWindowError("pipeline_window_not_found", 404);
        return { decision: "already_registered", recovery: concurrent, pipeline: nextPipeline, rowsWritten: 0, rowsRead: 0 };
      }
      throw new PipelineWindowError("recovery_supersede_conflict", 409);
    }
    const recovery = await getPipelineWindowRecovery(db, input.recoveryId);
    const nextPipeline = await readPipelineWindow(db, input.runKey);
    if (!recovery || !nextPipeline) throw new PipelineWindowError("recovery_supersede_not_found", 503);
    return {
      decision: "superseded",
      recovery,
      pipeline: nextPipeline,
      supersededRecoveryId: oldRecovery.recovery_id,
      rowsWritten: transaction.rowsWritten,
      rowsRead: transaction.rowsRead,
    };
  }

  let registrationResult: D1Result;
  try {
    registrationResult = await db.prepare(
      `INSERT INTO pipeline_window_recovery (
         recovery_id, run_key, plan_key, plan_sha256, scope_sha256,
         expected_attempt_count, expected_updated_at, previous_state_json,
         reason_code, approval_reference, authorized_at, expires_at, status,
         latest_receipt_key, latest_receipt_sha256, created_at, updated_at, completed_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTHORIZED', NULL, NULL, ?, ?, NULL)`,
    ).bind(
      input.recoveryId,
      input.runKey,
      input.planKey,
      input.planSha256,
      input.scopeSha256,
      input.expectedAttemptCount,
      input.expectedUpdatedAt,
      input.previousStateJson,
      input.reasonCode,
      input.approvalReference,
      input.authorizedAt,
      input.expiresAt,
      createdAt,
      createdAt,
    ).run();
  } catch (error: unknown) {
    // A concurrent identical registration is safe.  Re-read the immutable
    // row and reject any scope collision rather than guessing which plan won.
    const concurrent = await getPipelineWindowRecovery(db, input.recoveryId);
    if (concurrent && sameRegistration(concurrent, input)) {
      return { decision: "already_registered", recovery: concurrent, pipeline, rowsWritten: 0, rowsRead: 0 };
    }
    const concurrentByPlan = await db.prepare(
      `${selectRecoverySql()} WHERE run_key = ? AND plan_sha256 = ? LIMIT 1`,
    ).bind(input.runKey, input.planSha256).first<Record<string, unknown>>();
    if (concurrentByPlan) {
      const existing = serializeRecoveryRow(concurrentByPlan);
      if (existing.recovery_id === input.recoveryId && sameRegistration(existing, input)) {
        return { decision: "already_registered", recovery: existing, pipeline, rowsWritten: 0, rowsRead: 0 };
      }
      throw new PipelineWindowError("recovery_scope_conflict", 409);
    }
    console.error("pipeline recovery registration failed", error instanceof Error ? error.message : String(error));
    throw new PipelineWindowError("recovery_registration_failed", 503);
  }
  const recovery = await getPipelineWindowRecovery(db, input.recoveryId);
  if (!recovery) throw new PipelineWindowError("recovery_registration_not_found", 503);
  return {
    decision: "registered",
    recovery,
    pipeline,
    rowsWritten: rowsWritten(registrationResult),
    rowsRead: rowsRead(registrationResult),
  };
}

function makeClaimResult(
  recovery: PipelineWindowRecoveryRow,
  row: PipelineWindowRow,
  decision: PipelineRecoveryClaimDecision,
  rows: number,
  readRows = 0,
): PipelineRecoveryClaim {
  return {
    recoveryId: recovery.recovery_id,
    runKey: row.run_key,
    decision,
    recoveryStatus: recovery.status,
    status: row.status,
    leaseOwner: row.lease_owner,
    leaseExpiresAt: row.lease_expires_at,
    attemptCount: Number(row.attempt_count),
    rowsWritten: rows,
    rowsRead: readRows,
    recovery,
    row,
  };
}

/**
 * Atomically bind an approved recovery to one lease generation.  The first
 * claim uses the registration's expected attempt/update timestamp (CAS).
 * Later claims on the same recovery may take over only an expired lease and
 * always advance attempt_count, which makes late callbacks rejectable.
 */
export async function claimPipelineWindowRecovery(
  db: D1Like,
  input: PipelineRecoveryClaimInput,
  now = new Date(),
  leaseMs = PIPELINE_RECOVERY_LEASE_MS,
): Promise<PipelineRecoveryClaim> {
  validateClaim(input);
  if (!Number.isFinite(leaseMs) || leaseMs <= 0 || leaseMs > 60 * 60 * 1_000) {
    throw new PipelineWindowError("invalid_recovery_lease_duration", 422);
  }
  const nowIso = now.toISOString();
  const recovery = await getPipelineWindowRecovery(db, input.recoveryId);
  if (!recovery) throw new PipelineWindowError("recovery_not_found", 404);
  if (recovery.run_key !== input.runKey) throw new PipelineWindowError("recovery_run_key_mismatch", 409);
  if (recovery.status === "COMPLETED") {
    const row = await readPipelineWindow(db, input.runKey);
    if (!row) throw new PipelineWindowError("pipeline_window_not_found", 404);
    return makeClaimResult(recovery, row, "already_satisfied", 0);
  }
  if (recovery.status === "BLOCKED" || recovery.status === "REVOKED") {
    const row = await readPipelineWindow(db, input.runKey);
    if (!row) throw new PipelineWindowError("pipeline_window_not_found", 404);
    return makeClaimResult(recovery, row, "skip_blocked", 0);
  }
  if (Date.parse(recovery.expires_at) <= now.getTime()) {
    throw new PipelineWindowError("recovery_approval_expired", 409);
  }
  const row = await readPipelineWindow(db, input.runKey);
  if (!row) throw new PipelineWindowError("pipeline_window_not_found", 404);

  if (row.recovery_id && row.recovery_id !== recovery.recovery_id) {
    throw new PipelineWindowError("pipeline_window_recovery_bound", 409);
  }
  if (activeLease(row, nowIso)) {
    return makeClaimResult(recovery, row, "skip_leased", 0);
  }
  if (
    row.recovery_id === recovery.recovery_id &&
    (recovery.status === "ACTIVE" || recovery.status === "PAUSED") &&
    leaseExpired(row, nowIso)
  ) {
    // A previous recovery generation owns this row but its lease expired. A
    // new owner can take it over without changing the immutable authorization.
    const leaseExpiresAt = new Date(now.getTime() + leaseMs).toISOString();
    const transaction = await runAtomicPair(
      db,
      input.runKey,
      recovery.recovery_id,
      db.prepare(
        `UPDATE pipeline_window
            SET lease_owner = ?, lease_expires_at = ?, status = 'DISPATCHED',
                attempt_count = attempt_count + 1, last_error_code = NULL,
                github_run_id = COALESCE(NULLIF(?, ''), github_run_id),
                updated_at = ?, completed_at = NULL
          WHERE run_key = ? AND recovery_id = ?
            AND status IN ('DISPATCHED', 'RUNNING', 'FAILED_RETRYABLE')
            AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            AND EXISTS (
              SELECT 1 FROM pipeline_window_recovery
               WHERE recovery_id = ? AND status IN ('ACTIVE', 'PAUSED')
                 AND expires_at > ?
            )`,
      ).bind(
        input.leaseOwner,
        leaseExpiresAt,
        input.githubRunId ?? null,
        nowIso,
        input.runKey,
        recovery.recovery_id,
        nowIso,
        recovery.recovery_id,
        nowIso,
      ),
      db.prepare(
        `UPDATE pipeline_window_recovery
            SET status = 'ACTIVE', updated_at = ?
          WHERE recovery_id = ? AND status IN ('ACTIVE', 'PAUSED')
            AND expires_at > ?
            AND EXISTS (
              SELECT 1 FROM pipeline_window
               WHERE run_key = ? AND recovery_id = ? AND lease_owner = ?
                 AND status = 'DISPATCHED' AND updated_at = ?
            )`,
      ).bind(nowIso, recovery.recovery_id, nowIso, input.runKey, recovery.recovery_id, input.leaseOwner, nowIso),
    );
    const written = transaction?.rowsWritten ?? 0;
    const read = transaction?.rowsRead ?? 0;
    if (transaction) {
      const nextRow = await readPipelineWindow(db, input.runKey);
      const nextRecovery = await getPipelineWindowRecovery(db, input.recoveryId);
      if (!nextRow || !nextRecovery) throw new PipelineWindowError("recovery_claim_not_found", 503);
      return makeClaimResult(nextRecovery, nextRow, "takeover", written, read);
    }
    const latestRow = await readPipelineWindow(db, input.runKey);
    const latestRecovery = await getPipelineWindowRecovery(db, input.recoveryId);
    if (!latestRow || !latestRecovery) throw new PipelineWindowError("recovery_claim_not_found", 503);
    if (activeLease(latestRow, nowIso)) return makeClaimResult(latestRecovery, latestRow, "skip_leased", written, read);
    throw new PipelineWindowError("recovery_claim_conflict", 409);
  }

  const statusAllowed =
    row.status === "COMPLETED" ||
    (row.status === "FAILED_RETRYABLE" && leaseExpired(row, nowIso)) ||
    ((row.status === "DISPATCHED" || row.status === "RUNNING") && leaseExpired(row, nowIso)) ||
    (row.status === "BLOCKED_QUALITY" &&
      input.sourceFingerprint !== undefined &&
      input.sourceFingerprint !== null &&
      input.sourceFingerprint !== row.source_fingerprint);
  if (!statusAllowed || recovery.status !== "AUTHORIZED" || row.recovery_id !== null) {
    if (row.status === "BLOCKED_QUALITY") return makeClaimResult(recovery, row, "skip_blocked", 0);
    throw new PipelineWindowError("recovery_state_not_eligible", 409);
  }

  const leaseExpiresAt = new Date(now.getTime() + leaseMs).toISOString();
  const transaction = await runAtomicPair(
    db,
    input.runKey,
    recovery.recovery_id,
    db.prepare(
      `UPDATE pipeline_window
          SET recovery_id = ?, lease_owner = ?, lease_expires_at = ?,
              status = 'DISPATCHED', attempt_count = attempt_count + 1,
              source_fingerprint = COALESCE(NULLIF(?, ''), source_fingerprint),
              github_run_id = COALESCE(NULLIF(?, ''), github_run_id),
              last_error_code = NULL, updated_at = ?, completed_at = NULL
        WHERE run_key = ? AND recovery_id IS NULL
          AND attempt_count = ? AND updated_at = ?
          AND (
            status = 'COMPLETED' OR
            (status = 'FAILED_RETRYABLE' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) OR
            (status IN ('DISPATCHED', 'RUNNING') AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) OR
            (status = 'BLOCKED_QUALITY' AND ? IS NOT NULL AND ? <> COALESCE(source_fingerprint, ''))
          )
          AND EXISTS (
            SELECT 1 FROM pipeline_window_recovery
             WHERE recovery_id = ? AND run_key = ? AND status = 'AUTHORIZED'
               AND expires_at > ?
          )`,
    ).bind(
      recovery.recovery_id,
      input.leaseOwner,
      leaseExpiresAt,
      input.sourceFingerprint ?? null,
      input.githubRunId ?? null,
      nowIso,
      input.runKey,
      recovery.expected_attempt_count,
      recovery.expected_updated_at,
      nowIso,
      nowIso,
      input.sourceFingerprint ?? null,
      input.sourceFingerprint ?? null,
      recovery.recovery_id,
      input.runKey,
      nowIso,
    ),
    db.prepare(
      `UPDATE pipeline_window_recovery
          SET status = 'ACTIVE', updated_at = ?
        WHERE recovery_id = ? AND run_key = ? AND status = 'AUTHORIZED'
          AND expires_at > ?
          AND EXISTS (
            SELECT 1 FROM pipeline_window
             WHERE run_key = ? AND recovery_id = ? AND lease_owner = ?
               AND status = 'DISPATCHED' AND attempt_count = ?
               AND updated_at = ?
          )`,
    ).bind(
      nowIso,
      recovery.recovery_id,
      input.runKey,
      nowIso,
      input.runKey,
      recovery.recovery_id,
      input.leaseOwner,
      recovery.expected_attempt_count + 1,
      nowIso,
    ),
  );
  const written = transaction?.rowsWritten ?? 0;
  const read = transaction?.rowsRead ?? 0;
  if (transaction) {
    const nextRow = await readPipelineWindow(db, input.runKey);
    const nextRecovery = await getPipelineWindowRecovery(db, input.recoveryId);
    if (!nextRow || !nextRecovery) throw new PipelineWindowError("recovery_claim_not_found", 503);
    return makeClaimResult(nextRecovery, nextRow, "acquired", written, read);
  }

  const latestRow = await readPipelineWindow(db, input.runKey);
  const latestRecovery = await getPipelineWindowRecovery(db, input.recoveryId);
  if (!latestRow || !latestRecovery) throw new PipelineWindowError("recovery_claim_not_found", 503);
  if (latestRecovery.status === "COMPLETED") return makeClaimResult(latestRecovery, latestRow, "already_satisfied", written, read);
  if (activeLease(latestRow, nowIso)) return makeClaimResult(latestRecovery, latestRow, "skip_leased", written, read);
  if (latestRow.status === "BLOCKED_QUALITY") return makeClaimResult(latestRecovery, latestRow, "skip_blocked", written, read);
  throw new PipelineWindowError("recovery_claim_conflict", 409);
}

export type PipelineRecoveryMarkInput = {
  recoveryId: string;
  runKey: string;
  leaseOwner: string;
  expectedAttemptCount: number;
  status: "DISPATCHED" | "RUNNING" | "COMPLETED" | "FAILED_RETRYABLE" | "BLOCKED_QUALITY";
  errorCode?: string | null;
  sourceFingerprint?: string | null;
  githubRunId?: string | null;
  latestReceiptKey?: string | null;
  latestReceiptSha256?: string | null;
  /** Extend the active lease when a long-running runner is still healthy. */
  renewLease?: boolean;
};

function recoveryStatusForPipeline(status: PipelineRecoveryMarkInput["status"]): PipelineRecoveryStatus {
  if (status === "COMPLETED") return "COMPLETED";
  if (status === "BLOCKED_QUALITY") return "BLOCKED";
  if (status === "FAILED_RETRYABLE") return "PAUSED";
  return "ACTIVE";
}

/** Mark a recovery generation, rejecting late callbacks from old owners. */
export async function markPipelineWindowRecovery(
  db: D1Like,
  input: PipelineRecoveryMarkInput,
  now = new Date(),
): Promise<{ recovery: PipelineWindowRecoveryRow; row: PipelineWindowRow; rowsWritten: number; rowsRead: number }> {
  validateClaim({ recoveryId: input.recoveryId, runKey: input.runKey, leaseOwner: input.leaseOwner, sourceFingerprint: input.sourceFingerprint, githubRunId: input.githubRunId });
  requireSafeNonNegativeInteger(input.expectedAttemptCount, "invalid_expected_attempt_count");
  if (input.errorCode !== undefined && input.errorCode !== null && !ERROR_CODE.test(input.errorCode)) {
    throw new PipelineWindowError("invalid_error_code", 422);
  }
  if (input.latestReceiptSha256 !== undefined && input.latestReceiptSha256 !== null && !SHA256.test(input.latestReceiptSha256)) {
    throw new PipelineWindowError("invalid_receipt_sha256", 422);
  }
  if (input.renewLease !== undefined && typeof input.renewLease !== "boolean") {
    throw new PipelineWindowError("invalid_renew_lease", 422);
  }
  if (input.renewLease === true && input.status !== "RUNNING") {
    throw new PipelineWindowError("invalid_renew_lease", 422);
  }
  const nowIso = now.toISOString();
  const recovery = await getPipelineWindowRecovery(db, input.recoveryId);
  if (!recovery) throw new PipelineWindowError("recovery_not_found", 404);
  if (recovery.run_key !== input.runKey) throw new PipelineWindowError("recovery_run_key_mismatch", 409);
  const row = await readPipelineWindow(db, input.runKey);
  if (!row) throw new PipelineWindowError("pipeline_window_not_found", 404);
  if (recovery.status === "COMPLETED" && row.status === "COMPLETED") {
    return { recovery, row, rowsWritten: 0, rowsRead: 0 };
  }
  if (row.recovery_id !== input.recoveryId || row.lease_owner !== input.leaseOwner || Number(row.attempt_count) !== input.expectedAttemptCount) {
    throw new PipelineWindowError("recovery_status_conflict", 409);
  }
  if (row.lease_expires_at === null || row.lease_expires_at <= nowIso) {
    throw new PipelineWindowError("recovery_lease_expired", 409);
  }
  const terminal = input.status === "COMPLETED" || input.status === "BLOCKED_QUALITY" || input.status === "FAILED_RETRYABLE";
  const nextRecoveryStatus = recoveryStatusForPipeline(input.status);
  const completed = input.status === "COMPLETED" ? nowIso : null;
  const renewLease = input.status === "RUNNING" && input.renewLease === true;
  const renewedLeaseExpiresAt = new Date(now.getTime() + PIPELINE_RECOVERY_LEASE_MS).toISOString();
  const transaction = await runAtomicPair(
    db,
    input.runKey,
    input.recoveryId,
    db.prepare(
      `UPDATE pipeline_window
          SET status = ?, last_error_code = ?,
              source_fingerprint = COALESCE(NULLIF(?, ''), source_fingerprint),
              github_run_id = COALESCE(NULLIF(?, ''), github_run_id),
              lease_owner = CASE WHEN ? = 1 THEN NULL ELSE lease_owner END,
              lease_expires_at = CASE
                WHEN ? = 1 THEN NULL
                WHEN ? = 1 THEN ?
                ELSE lease_expires_at
              END,
              completed_at = CASE WHEN ? = 1 THEN ? ELSE completed_at END,
              updated_at = ?
        WHERE run_key = ? AND recovery_id = ? AND lease_owner = ?
          AND attempt_count = ? AND status <> 'COMPLETED'
          AND lease_expires_at IS NOT NULL AND lease_expires_at > ?`,
    ).bind(
      input.status,
      input.errorCode ?? null,
      input.sourceFingerprint ?? null,
      input.githubRunId ?? null,
      terminal ? 1 : 0,
      terminal ? 1 : 0,
      renewLease ? 1 : 0,
      renewedLeaseExpiresAt,
      input.status === "COMPLETED" ? 1 : 0,
      completed,
      nowIso,
      input.runKey,
      input.recoveryId,
      input.leaseOwner,
      input.expectedAttemptCount,
      nowIso,
    ),
    db.prepare(
      `UPDATE pipeline_window_recovery
          SET status = ?, latest_receipt_key = COALESCE(NULLIF(?, ''), latest_receipt_key),
              latest_receipt_sha256 = COALESCE(NULLIF(?, ''), latest_receipt_sha256),
              completed_at = CASE WHEN ? = 1 THEN ? ELSE completed_at END,
              updated_at = ?
        WHERE recovery_id = ? AND run_key = ? AND status IN ('AUTHORIZED', 'ACTIVE', 'PAUSED')
          AND EXISTS (
            SELECT 1 FROM pipeline_window
             WHERE run_key = ? AND recovery_id = ?
               AND status = ? AND attempt_count = ? AND updated_at = ?
          )`,
    ).bind(
      nextRecoveryStatus,
      input.latestReceiptKey ?? null,
      input.latestReceiptSha256 ?? null,
      input.status === "COMPLETED" ? 1 : 0,
      completed,
      nowIso,
      input.recoveryId,
      input.runKey,
      input.runKey,
      input.recoveryId,
      input.status,
      input.expectedAttemptCount,
      nowIso,
    ),
  );
  const written = transaction?.rowsWritten ?? 0;
  const read = transaction?.rowsRead ?? 0;
  if (!transaction) throw new PipelineWindowError("recovery_status_conflict", 409);
  const nextRow = await readPipelineWindow(db, input.runKey);
  const nextRecovery = await getPipelineWindowRecovery(db, input.recoveryId);
  if (!nextRow || !nextRecovery) throw new PipelineWindowError("recovery_status_not_found", 503);
  return { recovery: nextRecovery, row: nextRow, rowsWritten: written, rowsRead: read };
}
