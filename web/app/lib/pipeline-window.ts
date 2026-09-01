/**
 * Cross-scheduler idempotency and lease helpers for the daily market-data
 * pipeline.  This module intentionally contains only bounded D1 operations;
 * it must never read historical facts or construct a release.
 */

export const PIPELINE_NAME = "daily-market-data" as const;
export const PIPELINE_WINDOW_CONTRACT_VERSION: string = "v1";
export const PIPELINE_WINDOW_CONTRACT_VERSIONS = ["v1"] as const;
export const PIPELINE_WINDOW_LEASE_MS = 15 * 60 * 1_000;

export const PIPELINE_WINDOWS = [
  "evening",
  "morning_exact",
  "morning",
  "reconcile",
] as const;

export type PipelineWindowName = (typeof PIPELINE_WINDOWS)[number];

export type PipelineWindowStatus =
  | "DISPATCHED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED_RETRYABLE"
  | "BLOCKED_QUALITY";

export type PipelineWindowDecision =
  | "acquired"
  | "takeover"
  | "skip_completed"
  | "skip_leased"
  | "skip_blocked_quality";

export type PipelineWindowRow = {
  run_key: string;
  pipeline: string;
  window_date: string;
  window_name: string;
  contract_version: string;
  target_date: string | null;
  status: PipelineWindowStatus | string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  source_fingerprint: string | null;
  github_run_id: string | null;
  attempt_count: number;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type PipelineWindowInput = {
  runKey: string;
  windowDate: string;
  windowName: PipelineWindowName;
  contractVersion?: string;
  targetDate?: string | null;
  leaseOwner: string;
  sourceFingerprint?: string | null;
  githubRunId?: string | null;
};

export type PipelineWindowClaim = {
  runKey: string;
  decision: PipelineWindowDecision;
  status: string;
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  attemptCount: number;
  row: PipelineWindowRow;
  rowsWritten: number;
};

type D1Result = {
  meta?: { changes?: unknown; rows_read?: unknown; rows_written?: unknown };
};

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  run(): Promise<D1Result>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
};

type D1Like = {
  prepare(sql: string): D1Statement;
};

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const CONTRACT_VERSION = /^v\d+$/;
const OWNER = /^[A-Za-z0-9:_-]{8,120}$/;
const SOURCE_FINGERPRINT = /^[A-Za-z0-9._:/-]{1,256}$/;
const GITHUB_RUN_ID = /^\d{1,30}$/;

export function isPipelineWindowName(value: string): value is PipelineWindowName {
  return (PIPELINE_WINDOWS as readonly string[]).includes(value);
}

export function buildPipelineRunKey(
  windowDate: string,
  windowName: PipelineWindowName,
  contractVersion = PIPELINE_WINDOW_CONTRACT_VERSION,
): string {
  validateDate(windowDate, "invalid_window_date");
  validateWindowName(windowName);
  validateContractVersion(contractVersion);
  return `${PIPELINE_NAME}:${windowDate}:${windowName}:${contractVersion}`;
}

export function shanghaiDate(timestamp = Date.now()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const values = new Map(parts.map((part) => [part.type, part.value]));
  return `${values.get("year")}-${values.get("month")}-${values.get("day")}`;
}

export function parsePipelineRunKey(runKey: string): {
  windowDate: string;
  windowName: PipelineWindowName;
  contractVersion: string;
} | null {
  const match = /^(daily-market-data):(\d{4}-\d{2}-\d{2}):([a-z_]+):(v\d+)$/.exec(runKey);
  if (!match || !isPipelineWindowName(match[3])) return null;
  try {
    validateDate(match[2], "invalid_window_date");
    validateContractVersion(match[4]);
  } catch {
    return null;
  }
  return { windowDate: match[2], windowName: match[3], contractVersion: match[4] };
}

export function validatePipelineWindowInput(input: PipelineWindowInput): void {
  const contractVersion = input.contractVersion ?? PIPELINE_WINDOW_CONTRACT_VERSION;
  validateDate(input.windowDate, "invalid_window_date");
  validateWindowName(input.windowName);
  validateContractVersion(contractVersion);
  if (input.runKey !== buildPipelineRunKey(input.windowDate, input.windowName, contractVersion)) {
    throw new PipelineWindowError("run_key_mismatch", 422);
  }
  if (input.targetDate !== undefined && input.targetDate !== null) {
    validateDate(input.targetDate, "invalid_target_date");
  }
  if (!OWNER.test(input.leaseOwner)) throw new PipelineWindowError("invalid_lease_owner", 422);
  if (input.sourceFingerprint !== undefined && input.sourceFingerprint !== null && !SOURCE_FINGERPRINT.test(input.sourceFingerprint)) {
    throw new PipelineWindowError("invalid_source_fingerprint", 422);
  }
  if (input.githubRunId !== undefined && input.githubRunId !== null && !GITHUB_RUN_ID.test(input.githubRunId)) {
    throw new PipelineWindowError("invalid_github_run_id", 422);
  }
}

export class PipelineWindowError extends Error {
  readonly status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.name = "PipelineWindowError";
    this.status = status;
  }
}

function requireD1NonNegativeInteger(value: unknown, errorCode: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new PipelineWindowError(errorCode, 503);
  }
  return value;
}

function validateDate(value: string, errorCode: string): void {
  if (!ISO_DATE.test(value)) throw new PipelineWindowError(errorCode, 422);
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
    throw new PipelineWindowError(errorCode, 422);
  }
}

function validateWindowName(value: string): asserts value is PipelineWindowName {
  if (!isPipelineWindowName(value)) throw new PipelineWindowError("invalid_window_name", 422);
}

function validateContractVersion(value: string): void {
  if (!CONTRACT_VERSION.test(value)) throw new PipelineWindowError("invalid_contract_version", 422);
  if (!(PIPELINE_WINDOW_CONTRACT_VERSIONS as readonly string[]).includes(value)) {
    throw new PipelineWindowError("unknown_contract_version", 422);
  }
}

function rowDecision(row: PipelineWindowRow, nowIso: string): PipelineWindowDecision {
  if (row.status === "COMPLETED") return "skip_completed";
  if (row.status === "BLOCKED_QUALITY") return "skip_blocked_quality";
  if (
    (row.status === "DISPATCHED" || row.status === "RUNNING") &&
    row.lease_expires_at !== null &&
    row.lease_expires_at > nowIso
  ) {
    return "skip_leased";
  }
  // A conditional write that reports no changes can only be a protected
  // state. Keep this explicit so an unexpected status fails closed.
  return "skip_leased";
}

/**
 * Atomically claim one business window. The INSERT ... ON CONFLICT ... WHERE
 * statement is the critical invariant: there is no SELECT-then-UPDATE race.
 */
export async function claimPipelineWindow(
  db: D1Like,
  input: PipelineWindowInput,
  now = new Date(),
  leaseMs = PIPELINE_WINDOW_LEASE_MS,
): Promise<PipelineWindowClaim> {
  validatePipelineWindowInput(input);
  if (!Number.isFinite(leaseMs) || leaseMs <= 0 || leaseMs > 60 * 60 * 1_000) {
    throw new PipelineWindowError("invalid_lease_duration", 422);
  }
  const contractVersion = input.contractVersion ?? PIPELINE_WINDOW_CONTRACT_VERSION;
  const nowIso = now.toISOString();
  const leaseExpiresAt = new Date(now.getTime() + leaseMs).toISOString();
  const insertSql = `
    INSERT INTO pipeline_window (
      run_key, pipeline, window_date, window_name, contract_version,
      target_date, status, lease_owner, lease_expires_at, source_fingerprint,
      github_run_id, attempt_count, last_error_code, created_at, updated_at,
      completed_at
    ) VALUES (?, ?, ?, ?, ?, ?, 'DISPATCHED', ?, ?, ?, ?, 1, NULL, ?, ?, NULL)
    ON CONFLICT(run_key) DO UPDATE SET
      target_date = COALESCE(excluded.target_date, pipeline_window.target_date),
      lease_owner = excluded.lease_owner,
      lease_expires_at = excluded.lease_expires_at,
      status = 'DISPATCHED',
      source_fingerprint = COALESCE(NULLIF(excluded.source_fingerprint, ''), pipeline_window.source_fingerprint),
      github_run_id = COALESCE(excluded.github_run_id, pipeline_window.github_run_id),
      attempt_count = pipeline_window.attempt_count + 1,
      last_error_code = NULL,
      updated_at = excluded.updated_at,
      completed_at = NULL
    WHERE pipeline_window.status NOT IN ('COMPLETED', 'BLOCKED_QUALITY')
      AND (
        pipeline_window.status NOT IN ('DISPATCHED', 'RUNNING')
        OR pipeline_window.lease_expires_at IS NULL
        OR pipeline_window.lease_expires_at <= excluded.updated_at
        OR (
          pipeline_window.status = 'FAILED_RETRYABLE'
          AND (pipeline_window.lease_expires_at IS NULL OR pipeline_window.lease_expires_at <= excluded.updated_at)
        )
      )
      OR (
        pipeline_window.status = 'BLOCKED_QUALITY'
        AND NULLIF(excluded.source_fingerprint, '') IS NOT NULL
        AND NULLIF(excluded.source_fingerprint, '') <> COALESCE(NULLIF(pipeline_window.source_fingerprint, ''), '')
      )
  `;
  const result = await db.prepare(insertSql).bind(
    input.runKey,
    PIPELINE_NAME,
    input.windowDate,
    input.windowName,
    contractVersion,
    input.targetDate ?? null,
    input.leaseOwner,
    leaseExpiresAt,
    input.sourceFingerprint ?? null,
    input.githubRunId ?? null,
    nowIso,
    nowIso,
  ).run();
  // D1 rows_written includes physical index writes.  The claim updates
  // status/lease fields that have an index, so rows_written may be greater
  // than one even though the atomic UPSERT changed exactly one business row.
  // Use changes for the logical single-row invariant and keep rowsWritten as
  // budget telemetry returned to callers.
  const changes = requireD1NonNegativeInteger(
    result.meta?.changes,
    "pipeline_window_change_meta_invalid",
  );
  if (changes > 1) {
    throw new PipelineWindowError("pipeline_window_multirow_mutation", 503);
  }
  const rowsWritten = requireD1NonNegativeInteger(
    result.meta?.rows_written,
    "pipeline_window_write_meta_invalid",
  );
  const row = await db.prepare(
    `SELECT run_key, pipeline, window_date, window_name, contract_version,
            target_date, status, lease_owner, lease_expires_at,
            source_fingerprint, github_run_id, attempt_count, last_error_code,
            created_at, updated_at, completed_at
       FROM pipeline_window
      WHERE run_key = ?
      LIMIT 1`,
  ).bind(input.runKey).first<PipelineWindowRow>();
  if (!row) throw new PipelineWindowError("pipeline_window_not_found", 503);
  if (changes !== 1) {
    return {
      runKey: input.runKey,
      decision: rowDecision(row, nowIso),
      status: row.status,
      leaseOwner: row.lease_owner,
      leaseExpiresAt: row.lease_expires_at,
      attemptCount: Number(row.attempt_count ?? 0),
      row,
      rowsWritten,
    };
  }
  const decision: PipelineWindowDecision = Number(row.attempt_count ?? 0) <= 1 ? "acquired" : "takeover";
  return {
    runKey: input.runKey,
    decision,
    status: row.status,
    leaseOwner: row.lease_owner,
    leaseExpiresAt: row.lease_expires_at,
    attemptCount: Number(row.attempt_count ?? 0),
    row,
    rowsWritten,
  };
}

export async function getPipelineWindow(db: D1Like, runKey: string): Promise<PipelineWindowRow | null> {
  const parsed = parsePipelineRunKey(runKey);
  if (!parsed) throw new PipelineWindowError("invalid_run_key", 422);
  return db.prepare(
    `SELECT run_key, pipeline, window_date, window_name, contract_version,
            target_date, status, lease_owner, lease_expires_at,
            source_fingerprint, github_run_id, attempt_count, last_error_code,
            created_at, updated_at, completed_at
       FROM pipeline_window
      WHERE run_key = ?
      LIMIT 1`,
  ).bind(runKey).first<PipelineWindowRow>();
}

export async function markPipelineWindow(
  db: D1Like,
  input: {
    runKey: string;
    leaseOwner: string;
    status: PipelineWindowStatus;
    errorCode?: string | null;
    sourceFingerprint?: string | null;
    githubRunId?: string | null;
  },
  now = new Date(),
): Promise<PipelineWindowRow> {
  const parsed = parsePipelineRunKey(input.runKey);
  if (!parsed) throw new PipelineWindowError("invalid_run_key", 422);
  if (!OWNER.test(input.leaseOwner)) throw new PipelineWindowError("invalid_lease_owner", 422);
  if (input.errorCode !== undefined && input.errorCode !== null && !SOURCE_FINGERPRINT.test(input.errorCode)) {
    throw new PipelineWindowError("invalid_error_code", 422);
  }
  if (input.sourceFingerprint !== undefined && input.sourceFingerprint !== null && !SOURCE_FINGERPRINT.test(input.sourceFingerprint)) {
    throw new PipelineWindowError("invalid_source_fingerprint", 422);
  }
  if (input.githubRunId !== undefined && input.githubRunId !== null && !GITHUB_RUN_ID.test(input.githubRunId)) {
    throw new PipelineWindowError("invalid_github_run_id", 422);
  }
  const nowIso = now.toISOString();
  const terminal = input.status === "COMPLETED" || input.status === "BLOCKED_QUALITY";
  // A failed run has finished executing, so release its lease immediately;
  // the next watchdog window may then take over without waiting 15 minutes.
  const releaseLease = terminal || input.status === "FAILED_RETRYABLE";
  const result = await db.prepare(
    `UPDATE pipeline_window
        SET status = ?,
            last_error_code = ?,
            source_fingerprint = COALESCE(NULLIF(?, ''), source_fingerprint),
            github_run_id = COALESCE(NULLIF(?, ''), github_run_id),
            lease_owner = CASE WHEN ? = 1 THEN NULL ELSE lease_owner END,
            lease_expires_at = CASE WHEN ? = 1 THEN NULL ELSE lease_expires_at END,
            completed_at = CASE WHEN ? = 1 THEN ? ELSE completed_at END,
            updated_at = ?
      WHERE run_key = ? AND lease_owner = ?
        AND status <> 'COMPLETED'
        AND (lease_expires_at IS NULL OR lease_expires_at > ?)`,
  ).bind(
    input.status,
    input.errorCode ?? null,
    input.sourceFingerprint ?? null,
    input.githubRunId ?? null,
    releaseLease ? 1 : 0,
    releaseLease ? 1 : 0,
    input.status === "COMPLETED" ? 1 : 0,
    input.status === "COMPLETED" ? nowIso : null,
    nowIso,
    input.runKey,
    input.leaseOwner,
    nowIso,
  ).run();
  if (Number(result.meta?.changes ?? 0) !== 1) {
    throw new PipelineWindowError("pipeline_window_status_conflict", 409);
  }
  const row = await getPipelineWindow(db, input.runKey);
  if (!row) throw new PipelineWindowError("pipeline_window_not_found", 404);
  return row;
}
