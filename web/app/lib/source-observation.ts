/**
 * Producer-side source observation receipts for v0.5 shadow mode.
 *
 * The receipt is deliberately content-addressed and written to R2 before the
 * mutable D1 projection.  A transient D1 failure therefore cannot erase the
 * official-source evidence; a later bounded reconcile can materialise the
 * same receipt into `source_observation` without downloading the source
 * again.
 */

export const SOURCE_OBSERVATION_SCHEMA_VERSION = 1 as const;
export const SOURCE_OBSERVATION_CONTRACT_VERSION = "flow-product-v0.5" as const;
export const SOURCE_OBSERVATION_RECEIPT_PREFIX = "market-data/source-observations";
export const MAX_SOURCE_OBSERVATION_EVIDENCE_BYTES = 32 * 1024;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const RUN_KEY = /^daily-market-data:\d{4}-\d{2}-\d{2}:(?:evening|morning_exact|morning|reconcile):v\d+$/;
const IDENTIFIER = /^[a-z][a-z0-9_]{1,119}$/;
const REDACTED = "[REDACTED]";

export type SourceObservationStatus =
  | "NOT_PUBLISHED"
  | "SOURCE_UNAVAILABLE"
  | "OBSERVED"
  | "VERIFIED"
  | "REJECTED_QUALITY"
  | "SUPERSEDED_BY_REVISION";

type Primitive = string | number | boolean | null;
type JsonObject = Record<string, unknown>;

export type SourceObservationNormalizedFile = {
  name: string;
  sha256: string;
  rowCount: number;
  tradeDates: string[];
};

export type SourceObservationArtifact = {
  sourceId: string;
  dataset: string;
  targetDate: string;
  requestedDate: string;
  tradeDates: string[];
  observedAt: string;
  sourceUrl: string;
  dateBasis: string;
  sha256: string;
  contentType: string;
  rawKey: string;
  rawBytes: number;
  metadata: JsonObject;
  releaseId: string;
  normalizedFiles?: SourceObservationNormalizedFile[];
  archiveRunId?: string;
  runKey?: string | null;
};

export type SourceObservationReceipt = {
  schemaVersion: 1;
  observationId: string;
  sourceId: string;
  dataset: string;
  targetDate: string;
  sourceBusinessDate: string | null;
  publishedDate: string | null;
  dateBasis: string;
  status: SourceObservationStatus;
  officialUrl: string;
  requestParams: JsonObject;
  httpStatus: number | null;
  responseContentType: string | null;
  rawObjectKey: string | null;
  rawBytes: number | null;
  rawSha256: string | null;
  normalizedSha256: string | null;
  sourceFingerprint: string;
  contractVersion: typeof SOURCE_OBSERVATION_CONTRACT_VERSION;
  runKey: string | null;
  observedAt: string;
  reasonCode: string | null;
  evidence: JsonObject;
  createdAt: string;
};

type SourceObservationD1 = {
  prepare(sql: string): {
    bind(...values: unknown[]): { run(): Promise<{ meta?: { changes?: unknown } }> };
  };
};

type SourceObservationR2 = {
  head?(key: string): Promise<{
    size?: number;
    customMetadata?: Record<string, string>;
  } | null>;
  put(
    key: string,
    value: Uint8Array | string,
    options?: ObservationR2PutOptions,
  ): Promise<unknown>;
};

type ObservationR2PutOptions = {
  httpMetadata?: Record<string, string>;
  customMetadata?: Record<string, string>;
};

export type SourceObservationPersistResult = {
  sourceFingerprint: string;
  observationId: string;
  receiptKey: string;
  receiptSha256: string;
  r2Status: "created" | "existing";
  d1Status: "recorded" | "already_recorded" | "unavailable";
};

function isRecord(value: unknown): value is JsonObject {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function canonicalDate(value: unknown): string | null {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return null;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value
    ? null
    : value;
}

function canonicalObservedAt(value: string): string {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? new Date().toISOString() : new Date(parsed).toISOString();
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

function boundedValue(value: unknown, depth = 0): Primitive | JsonObject | Primitive[] {
  if (depth > 5) return "[TRUNCATED]";
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return typeof value === "string" && value.length > 512 ? `${value.slice(0, 509)}...` : value;
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (Array.isArray(value)) return value.slice(0, 32).map((item) => boundedValue(item, depth + 1) as Primitive);
  if (!isRecord(value)) return "[UNSUPPORTED]";
  const output: JsonObject = {};
  for (const key of Object.keys(value).sort().slice(0, 64)) {
    output[key] = /token|secret|password|cookie|authorization|api[_-]?key|access[_-]?key/i.test(key)
      ? REDACTED
      : boundedValue(value[key], depth + 1);
  }
  return output;
}

function boundedRecord(value: unknown): JsonObject {
  const candidate = boundedValue(isRecord(value) ? value : {}, 0);
  return isRecord(candidate) ? candidate : {};
}

function metadataValue(metadata: JsonObject, ...keys: string[]): unknown {
  for (const key of keys) {
    if (metadata[key] !== undefined && metadata[key] !== null) return metadata[key];
  }
  return null;
}

function metadataStatus(metadata: JsonObject): number | null {
  const value = metadataValue(metadata, "status_code", "statusCode", "http_status", "httpStatus");
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 100 || value > 599) return null;
  return value;
}

function sourceBusinessDate(input: SourceObservationArtifact): string | null {
  const explicit = canonicalDate(
    metadataValue(
      input.metadata,
      "source_business_date",
      "sourceBusinessDate",
      "source_trade_date",
      "sourceTradeDate",
      "trade_date",
      "tradeDate",
    ),
  );
  if (explicit) return explicit;
  const dates = [...new Set(input.tradeDates.map(canonicalDate).filter((date): date is string => Boolean(date)))].sort();
  return dates.length === 1 ? dates[0] : null;
}

function publishedDate(input: SourceObservationArtifact): string | null {
  return canonicalDate(
    metadataValue(
      input.metadata,
      "published_date",
      "publishedDate",
      "publication_date",
      "publicationDate",
      "source_published_date",
    ),
  );
}

function validateInput(input: SourceObservationArtifact): void {
  if (!IDENTIFIER.test(input.sourceId) || !IDENTIFIER.test(input.dataset)) {
    throw new Error("source_observation_invalid_identity");
  }
  if (!canonicalDate(input.targetDate)) throw new Error("source_observation_invalid_target_date");
  if (!/^[A-Za-z0-9._:/-]{1,240}$/.test(input.rawKey)) {
    throw new Error("source_observation_invalid_raw_key");
  }
  if (!SHA256.test(input.sha256)) throw new Error("source_observation_invalid_raw_sha256");
  if (!Number.isSafeInteger(input.rawBytes) || input.rawBytes < 0) {
    throw new Error("source_observation_invalid_raw_bytes");
  }
  if (!input.sourceUrl.startsWith("https://") || input.sourceUrl.length > 2048) {
    throw new Error("source_observation_invalid_official_url");
  }
  if (!input.dateBasis || input.dateBasis.length > 160) throw new Error("source_observation_invalid_date_basis");
  if (input.runKey !== undefined && input.runKey !== null && !RUN_KEY.test(input.runKey)) {
    throw new Error("source_observation_invalid_run_key");
  }
}

export async function sha256Hex(content: Uint8Array): Promise<string> {
  const bytes = content.buffer.slice(content.byteOffset, content.byteOffset + content.byteLength) as ArrayBuffer;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function buildSourceObservationReceipt(
  input: SourceObservationArtifact,
): Promise<SourceObservationReceipt> {
  validateInput(input);
  const observedAt = canonicalObservedAt(input.observedAt);
  // Keep the serialized receipt stable on replay.  A wall-clock creation
  // timestamp would make the same fingerprint produce different bytes and
  // turn an idempotent retry into an immutable-object conflict.
  const createdAt = observedAt;
  const sourceDate = sourceBusinessDate(input);
  const published = publishedDate(input);
  const statusCode = metadataStatus(input.metadata);
  const normalizedFiles = (input.normalizedFiles ?? []).map((file) => ({
    name: file.name,
    sha256: file.sha256,
    rowCount: file.rowCount,
    tradeDates: [...new Set(file.tradeDates.map(canonicalDate).filter((date): date is string => Boolean(date)))].sort(),
  }));
  const normalizedSha256 = normalizedFiles.length === 1 ? normalizedFiles[0].sha256 : null;
  if (normalizedSha256 !== null && !SHA256.test(normalizedSha256)) {
    throw new Error("source_observation_invalid_normalized_sha256");
  }

  let status: SourceObservationStatus;
  let reasonCode: string | null = null;
  if (sourceDate) {
    status = normalizedFiles.length > 0 ? "VERIFIED" : "OBSERVED";
  } else if (statusCode === 404) {
    status = "NOT_PUBLISHED";
    reasonCode = "source_not_published";
  } else if (statusCode !== null && statusCode >= 400) {
    status = "SOURCE_UNAVAILABLE";
    reasonCode = "source_http_error";
  } else {
    status = "SOURCE_UNAVAILABLE";
    reasonCode = "source_business_date_unknown";
  }

  // The fingerprint is intentionally independent of wall-clock observation
  // time, runner ids, URLs and redacted request parameters.  A replay of the
  // same official bytes for the same semantic date is therefore one fact.
  const fingerprintInput = {
    contractVersion: SOURCE_OBSERVATION_CONTRACT_VERSION,
    sourceId: input.sourceId,
    dataset: input.dataset,
    targetDate: input.targetDate,
    requestedDate: input.requestedDate,
    sourceBusinessDate: sourceDate,
    publishedDate: published,
    dateBasis: input.dateBasis,
    rawSha256: input.sha256,
  };
  const sourceFingerprint = await sha256Hex(new TextEncoder().encode(canonicalJson(fingerprintInput)));
  const observationId = `obs:${input.sourceId}:${input.dataset}:${input.targetDate}:${sourceFingerprint.slice(0, 32)}`;
  const requestParams = boundedRecord(metadataValue(input.metadata, "request_params", "requestParams"));
  const evidence: JsonObject = {
    archiveRunId: input.archiveRunId ?? null,
    artifactReleaseId: input.releaseId,
    requestedDate: input.requestedDate,
    rawObjectKey: input.rawKey,
    rawSha256: input.sha256,
    rawBytes: input.rawBytes,
    tradeDates: [...new Set(input.tradeDates.map(canonicalDate).filter((date): date is string => Boolean(date)))].sort(),
    normalizedFiles,
    receiptStage: "history-import",
  };
  const evidenceBytes = new TextEncoder().encode(canonicalJson(evidence));
  if (evidenceBytes.byteLength > MAX_SOURCE_OBSERVATION_EVIDENCE_BYTES) {
    throw new Error("source_observation_evidence_too_large");
  }

  return {
    schemaVersion: SOURCE_OBSERVATION_SCHEMA_VERSION,
    observationId,
    sourceId: input.sourceId,
    dataset: input.dataset,
    targetDate: input.targetDate,
    sourceBusinessDate: sourceDate,
    publishedDate: published,
    dateBasis: input.dateBasis,
    status,
    officialUrl: input.sourceUrl,
    requestParams,
    httpStatus: statusCode,
    responseContentType: input.contentType || null,
    rawObjectKey: input.rawKey,
    rawBytes: input.rawBytes,
    rawSha256: input.sha256,
    normalizedSha256,
    sourceFingerprint,
    contractVersion: SOURCE_OBSERVATION_CONTRACT_VERSION,
    runKey: input.runKey ?? null,
    observedAt,
    reasonCode,
    evidence,
    createdAt,
  };
}

function receiptKey(receipt: SourceObservationReceipt): string {
  return `${SOURCE_OBSERVATION_RECEIPT_PREFIX}/${receipt.targetDate}/${receipt.sourceId}/${receipt.dataset}/${receipt.sourceFingerprint}.json`;
}

export async function putImmutableObservationReceipt(
  bucket: SourceObservationR2,
  receipt: SourceObservationReceipt,
): Promise<{ key: string; sha256: string; status: "created" | "existing" }> {
  const key = receiptKey(receipt);
  const body = new TextEncoder().encode(canonicalJson(receipt));
  const bodySha256 = await sha256Hex(body);
  const head = typeof bucket.head === "function" ? await bucket.head(key) : null;
  const matches = (existing: { size?: number; customMetadata?: Record<string, string> } | null): boolean => {
    if (!existing) return false;
    const metadata = existing.customMetadata ?? {};
    // observedAt is intentionally retained in the receipt but excluded from
    // the fingerprint.  A later replay may therefore have different bytes
    // while still referring to the same immutable fact; never overwrite the
    // first object or turn that harmless replay into a conflict.
    return metadata.sourceFingerprint === receipt.sourceFingerprint &&
      metadata.sourceId === receipt.sourceId &&
      metadata.dataset === receipt.dataset &&
      metadata.targetDate === receipt.targetDate;
  };
  if (head) {
    if (!matches(head)) throw new Error("source_observation_receipt_conflict");
    return { key, sha256: bodySha256, status: "existing" };
  }
  try {
    await bucket.put(key, body, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        sourceFingerprint: receipt.sourceFingerprint,
        sha256: bodySha256,
        sourceId: receipt.sourceId,
        dataset: receipt.dataset,
        targetDate: receipt.targetDate,
      },
    });
    return { key, sha256: bodySha256, status: "created" };
  } catch (error) {
    const raced = typeof bucket.head === "function" ? await bucket.head(key) : null;
    if (matches(raced)) return { key, sha256: bodySha256, status: "existing" };
    throw error;
  }
}

export async function recordSourceObservation(
  bucket: SourceObservationR2,
  db: SourceObservationD1 | undefined,
  input: SourceObservationArtifact,
): Promise<SourceObservationPersistResult> {
  const receipt = await buildSourceObservationReceipt(input);
  const stored = await putImmutableObservationReceipt(bucket, receipt);
  let d1Status: SourceObservationPersistResult["d1Status"] = "unavailable";
  if (db) {
    try {
      const result = await db.prepare(
        `INSERT INTO source_observation (
           observation_id, source_id, dataset, target_date,
           source_business_date, published_date, date_basis, status,
           official_url, request_params_json, http_status,
           response_content_type, raw_object_key, raw_bytes, raw_sha256,
           normalized_sha256, source_fingerprint, contract_version, run_key,
           observed_at, reason_code, evidence_json, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(source_fingerprint) DO NOTHING`,
      ).bind(
        receipt.observationId,
        receipt.sourceId,
        receipt.dataset,
        receipt.targetDate,
        receipt.sourceBusinessDate,
        receipt.publishedDate,
        receipt.dateBasis,
        receipt.status,
        receipt.officialUrl,
        canonicalJson(receipt.requestParams),
        receipt.httpStatus,
        receipt.responseContentType,
        receipt.rawObjectKey,
        receipt.rawBytes,
        receipt.rawSha256,
        receipt.normalizedSha256,
        receipt.sourceFingerprint,
        receipt.contractVersion,
        receipt.runKey,
        receipt.observedAt,
        receipt.reasonCode,
        canonicalJson(receipt.evidence),
        receipt.createdAt,
      ).run();
      const changes = Number(result.meta?.changes ?? 0);
      d1Status = changes === 1 ? "recorded" : "already_recorded";
    } catch {
      // The immutable R2 receipt is the recovery source.  D1 is only the
      // bounded query projection and can be reconciled later.
      d1Status = "unavailable";
    }
  }
  return {
    sourceFingerprint: receipt.sourceFingerprint,
    observationId: receipt.observationId,
    receiptKey: stored.key,
    receiptSha256: stored.sha256,
    r2Status: stored.status,
    d1Status,
  };
}
