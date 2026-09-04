/**
 * Small, immutable recovery completion receipts.
 *
 * The Worker only verifies a bounded JSON object and its R2 checksum.  It does
 * not enumerate historical facts or rebuild a read model in a request.  Large
 * reconciliation remains an offline job; D1 stores only the verified key and
 * hash through the existing recovery status mutation.
 */

import { parsePipelineRunKey, PipelineWindowError } from "./pipeline-window.ts";

export { PipelineWindowError };

const SHA256 = /^[0-9a-f]{64}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const IDENTIFIER = /^[A-Za-z0-9:_./-]{1,220}$/;
const RECEIPT_KEY = /^market-data\/recovery\/receipts\/[A-Za-z0-9._:-]{1,120}\/\d{4}-\d{2}-\d{2}\/[a-z_]{1,64}\/[0-9]{1,12}\.json$/;
const MAX_RECEIPT_BYTES = 256 * 1024;
const RECOVERY_STAGES = new Set([
  "core_collection",
  "dashboard",
  "etf_penetration",
  "stock_flow",
  "pcf_exact",
  "publish",
]);

type RecordValue = Record<string, unknown>;

export type RecoveryReceiptExpectation = {
  recoveryId: string;
  runKey: string;
  planSha256: string;
  attemptCount: number;
};

export type RecoveryReceipt = {
  schemaVersion: 1;
  campaignId: string;
  recoveryId: string;
  planHash: string;
  runKey: string;
  attemptCount: number;
  dataDate: string;
  market: string;
  stage: string;
  status: "COMPLETED" | "FAILED_RETRYABLE" | "BLOCKED_QUALITY" | "SOURCE_UNAVAILABLE";
  inputObjects: Array<{
    key: string;
    sha256: string;
    sourceDates: string[];
    sourceFingerprint: string;
  }>;
  versions: { schema: string; calculation: string; classification: string };
  producerBuildSha: string;
  collectorRunId: string;
  publishRunId: string | null;
  output: {
    manifestSha256: string;
    releaseId: string;
    slot: string;
    asOf: string;
  } | null;
  qa: { result: "PASS" | "FAIL" | "BLOCKED" | "UNKNOWN"; evidenceKeys: string[] };
  historical: { dateReadable: boolean; activeBefore: string | null; activeAfter: string | null };
  resources: {
    d1RowsRead: number;
    d1RowsWritten: number;
    workerRequests: number;
    r2ClassA: number;
    r2ClassB: number;
    r2Bytes: number;
    actionsMinutes: number;
    measuredAt: string;
  };
  completedAt: string | null;
};

type R2Head = { customMetadata?: Record<string, string>; size?: number } | null;
type R2Body = { arrayBuffer(): Promise<ArrayBuffer> };
type R2BucketLike = {
  head(key: string): Promise<R2Head>;
  get(key: string): Promise<R2Body | null>;
};

function isRecord(value: unknown): value is RecordValue {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function requiredString(value: unknown, code: string): string {
  if (typeof value !== "string" || value.length === 0 || !IDENTIFIER.test(value)) {
    throw new PipelineWindowError(code, 422);
  }
  return value;
}

function hash(value: unknown, code: string): string {
  if (typeof value !== "string" || !SHA256.test(value.toLowerCase())) {
    throw new PipelineWindowError(code, 422);
  }
  return value.toLowerCase();
}

function isoDate(value: unknown, code: string): string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) throw new PipelineWindowError(code, 422);
  const parsed = new Date(`${value}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new PipelineWindowError(code, 422);
  }
  return value;
}

function isoInstant(value: unknown, code: string): string {
  if (typeof value !== "string" || !ISO_INSTANT.test(value) || Number.isNaN(Date.parse(value))) {
    throw new PipelineWindowError(code, 422);
  }
  return value;
}

function nonNegative(value: unknown, code: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new PipelineWindowError(code, 422);
  return Number(value);
}

function inputObjects(value: unknown): RecoveryReceipt["inputObjects"] {
  if (!Array.isArray(value) || value.length === 0) throw new PipelineWindowError("invalid_receipt_input_objects", 422);
  return value.map((item) => {
    if (!isRecord(item)) throw new PipelineWindowError("invalid_receipt_input_object", 422);
    const sourceDates = item.sourceDates;
    if (!Array.isArray(sourceDates) || sourceDates.length === 0) throw new PipelineWindowError("invalid_receipt_source_dates", 422);
    return {
      key: requiredString(item.key, "invalid_receipt_input_key"),
      sha256: hash(item.sha256, "invalid_receipt_input_sha256"),
      sourceDates: sourceDates.map((entry) => isoDate(entry, "invalid_receipt_source_date")),
      sourceFingerprint: requiredString(item.sourceFingerprint, "invalid_receipt_source_fingerprint"),
    };
  });
}

function versions(value: unknown): RecoveryReceipt["versions"] {
  if (!isRecord(value)) throw new PipelineWindowError("invalid_receipt_versions", 422);
  return {
    schema: requiredString(value.schema, "invalid_receipt_schema_version"),
    calculation: requiredString(value.calculation, "invalid_receipt_calculation_version"),
    classification: requiredString(value.classification, "invalid_receipt_classification_version"),
  };
}

function qa(value: unknown): RecoveryReceipt["qa"] {
  if (!isRecord(value)) throw new PipelineWindowError("invalid_receipt_qa", 422);
  const result = String(value.result || "").toUpperCase() as RecoveryReceipt["qa"]["result"];
  if (!["PASS", "FAIL", "BLOCKED", "UNKNOWN"].includes(result)) throw new PipelineWindowError("invalid_receipt_qa_result", 422);
  if (!Array.isArray(value.evidenceKeys) || value.evidenceKeys.some((entry) => typeof entry !== "string" || entry.length === 0)) {
    throw new PipelineWindowError("invalid_receipt_qa_evidence", 422);
  }
  if (result === "PASS" && value.evidenceKeys.length === 0) throw new PipelineWindowError("receipt_pass_evidence_missing", 422);
  return { result, evidenceKeys: value.evidenceKeys as string[] };
}

function output(value: unknown): NonNullable<RecoveryReceipt["output"]> {
  if (!isRecord(value)) throw new PipelineWindowError("invalid_receipt_output", 422);
  return {
    manifestSha256: hash(value.manifestSha256, "invalid_receipt_manifest_sha256"),
    releaseId: requiredString(value.releaseId, "invalid_receipt_release_id"),
    slot: requiredString(value.slot, "invalid_receipt_slot"),
    asOf: isoDate(value.asOf, "invalid_receipt_output_as_of"),
  };
}

function historical(value: unknown): RecoveryReceipt["historical"] {
  if (!isRecord(value) || typeof value.dateReadable !== "boolean") throw new PipelineWindowError("invalid_receipt_historical", 422);
  if (value.activeBefore !== null && value.activeBefore !== undefined && typeof value.activeBefore !== "string") throw new PipelineWindowError("invalid_receipt_active_before", 422);
  if (value.activeAfter !== null && value.activeAfter !== undefined && typeof value.activeAfter !== "string") throw new PipelineWindowError("invalid_receipt_active_after", 422);
  return {
    dateReadable: value.dateReadable,
    activeBefore: value.activeBefore === undefined ? null : value.activeBefore as string | null,
    activeAfter: value.activeAfter === undefined ? null : value.activeAfter as string | null,
  };
}

function resources(value: unknown): RecoveryReceipt["resources"] {
  if (!isRecord(value)) throw new PipelineWindowError("invalid_receipt_resources", 422);
  return {
    d1RowsRead: nonNegative(value.d1RowsRead, "invalid_receipt_d1_rows_read"),
    d1RowsWritten: nonNegative(value.d1RowsWritten, "invalid_receipt_d1_rows_written"),
    workerRequests: nonNegative(value.workerRequests, "invalid_receipt_worker_requests"),
    r2ClassA: nonNegative(value.r2ClassA, "invalid_receipt_r2_class_a"),
    r2ClassB: nonNegative(value.r2ClassB, "invalid_receipt_r2_class_b"),
    r2Bytes: nonNegative(value.r2Bytes, "invalid_receipt_r2_bytes"),
    actionsMinutes: nonNegative(value.actionsMinutes, "invalid_receipt_actions_minutes"),
    measuredAt: isoInstant(value.measuredAt, "invalid_receipt_measured_at"),
  };
}

export function validateRecoveryReceipt(
  value: unknown,
  expected?: RecoveryReceiptExpectation,
): RecoveryReceipt {
  if (!isRecord(value) || value.schemaVersion !== 1) throw new PipelineWindowError("invalid_recovery_receipt", 422);
  const status = String(value.status || "").toUpperCase() as RecoveryReceipt["status"];
  if (!["COMPLETED", "FAILED_RETRYABLE", "BLOCKED_QUALITY", "SOURCE_UNAVAILABLE"].includes(status)) throw new PipelineWindowError("invalid_receipt_status", 422);
  const receipt: RecoveryReceipt = {
    schemaVersion: 1,
    campaignId: requiredString(value.campaignId, "invalid_receipt_campaign_id"),
    recoveryId: requiredString(value.recoveryId, "invalid_receipt_recovery_id"),
    planHash: hash(value.planHash, "invalid_receipt_plan_hash"),
    runKey: requiredString(value.runKey, "invalid_receipt_run_key"),
    attemptCount: nonNegative(value.attemptCount, "invalid_receipt_attempt_count"),
    dataDate: isoDate(value.dataDate, "invalid_receipt_data_date"),
    market: requiredString(value.market, "invalid_receipt_market"),
    stage: requiredString(value.stage, "invalid_receipt_stage"),
    status,
    inputObjects: inputObjects(value.inputObjects),
    versions: versions(value.versions),
    producerBuildSha: hash(value.producerBuildSha, "invalid_receipt_producer_build_sha"),
    collectorRunId: requiredString(value.collectorRunId, "invalid_receipt_collector_run_id"),
    publishRunId: value.publishRunId === null || value.publishRunId === undefined ? null : requiredString(value.publishRunId, "invalid_receipt_publish_run_id"),
    output: value.output === null || value.output === undefined ? null : output(value.output),
    qa: qa(value.qa),
    historical: historical(value.historical),
    resources: resources(value.resources),
    completedAt: value.completedAt === null || value.completedAt === undefined ? null : isoInstant(value.completedAt, "invalid_receipt_completed_at"),
  };
  if (!parsePipelineRunKey(receipt.runKey)) throw new PipelineWindowError("invalid_receipt_run_key", 422);
  if (!RECOVERY_STAGES.has(receipt.stage)) throw new PipelineWindowError("unknown_receipt_stage", 422);
  if (expected) {
    if (receipt.recoveryId !== expected.recoveryId || receipt.runKey !== expected.runKey || receipt.planHash !== expected.planSha256.toLowerCase()) {
      throw new PipelineWindowError("receipt_scope_binding_mismatch", 409);
    }
    if (receipt.attemptCount !== expected.attemptCount) throw new PipelineWindowError("receipt_attempt_mismatch", 409);
  }
  if (receipt.status === "COMPLETED") {
    if (receipt.qa.result !== "PASS" || !receipt.output || !receipt.historical.dateReadable || !receipt.completedAt) {
      throw new PipelineWindowError("receipt_completion_evidence_incomplete", 409);
    }
    if (receipt.output.asOf !== receipt.dataDate) throw new PipelineWindowError("receipt_output_date_mismatch", 409);
  } else if (receipt.completedAt) {
    throw new PipelineWindowError("nonterminal_receipt_completed_at", 422);
  }
  return receipt;
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

/** Verify the immutable R2 object and then validate the receipt binding. */
export async function verifyRecoveryReceipt(
  bucket: R2BucketLike,
  key: string,
  expectedSha256: string,
  expected: RecoveryReceiptExpectation,
): Promise<RecoveryReceipt> {
  if (!RECEIPT_KEY.test(key) || key.includes("..") || key.includes("//")) throw new PipelineWindowError("invalid_receipt_key", 422);
  const sha256 = hash(expectedSha256, "invalid_receipt_sha256");
  const head = await bucket.head(key);
  if (!head || head.size === undefined || head.size > MAX_RECEIPT_BYTES || head.customMetadata?.sha256 !== sha256) {
    throw new PipelineWindowError("receipt_object_not_verified", 409);
  }
  const object = await bucket.get(key);
  if (!object) throw new PipelineWindowError("receipt_object_not_found", 409);
  const bytes = await object.arrayBuffer();
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_RECEIPT_BYTES) throw new PipelineWindowError("receipt_object_size_invalid", 409);
  if (await sha256Hex(bytes) !== sha256) throw new PipelineWindowError("receipt_sha256_mismatch", 409);
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new PipelineWindowError("receipt_json_invalid", 409);
  }
  return validateRecoveryReceipt(value, expected);
}
