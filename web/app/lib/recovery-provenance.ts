/**
 * Bounded provenance repair for legacy history snapshots.
 *
 * The history-import endpoint has always archived immutable raw and normalized
 * objects, but older runs did not persist the source_readiness and
 * history_artifact_pointer receipts consumed by the recovery inventory. This
 * module reconstructs only those metadata receipts from the exact manifest
 * key stored with each historical snapshot. It never lists R2, downloads raw
 * or normalized bodies, writes release/read-model facts, or changes an active
 * pointer.
 */

export const RECOVERY_PROVENANCE_SCHEMA_VERSION = 1 as const;
export const RECOVERY_PROVENANCE_CONTRACT = "recovery-provenance-repair-v1" as const;
export const RECOVERY_PROVENANCE_SCOPE_START = "2024-08-01" as const;
export const RECOVERY_PROVENANCE_SCOPE_END = "2026-09-03" as const;
export const DEFAULT_PROVENANCE_LIMIT = 2;
export const MAX_PROVENANCE_LIMIT = 4;
export const MAX_MANIFEST_BYTES = 512_000;
export const MAX_D1_ROWS_READ = 8;
export const MAX_D1_QUERIES = 1;
export const MAX_R2_GETS = MAX_PROVENANCE_LIMIT;
export const MAX_R2_HEADS = 64;
export const MAX_REPAIR_STATEMENTS = 200;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?(?:Z|[+-]\d{2}:\d{2})$/;
const SHA256 = /^[a-f0-9]{64}$/;
const SAFE_SEGMENT = /^[A-Za-z0-9._:-]{1,160}$/;
const RAW_KEY = /^market-data\/raw\/([a-z0-9_]+)\/([a-z0-9_]+)\/([^/]+)\/([a-f0-9]{64})\.([a-z0-9]{1,8})$/;
const NORMALIZED_KEY = /^market-data\/normalized\/([a-z0-9_]+)\/([a-z0-9_]+)\/([^/]+)\/([a-f0-9]{64})\/([A-Za-z0-9._-]{1,120})\.csv(?:\.gz)?$/;
const HISTORY_MANIFEST_KEY = /^market-data\/history-manifests\/([^/]{1,160})\/(\d{4}-\d{2}-\d{2})\.json$/;
const HISTORY_SNAPSHOT_KEY = /^market-data\/history\/\d{4}-\d{2}-\d{2}\/[a-f0-9]{64}\.json$/;

const DATASETS = new Set([
  "etf_scale",
  "etf_nav",
  "etf_pcf",
  "margin_market_daily",
  "margin_security_daily",
  "stock_connect_daily",
]);
const SOURCES = new Set(["sse", "szse", "bse", "hkex"]);
const REQUIRED_DATASETS = new Set(["etf_scale", "margin_market_daily", "stock_connect_daily"]);

type D1Meta = { rows_read?: unknown; rows_written?: unknown };
type D1Result<T> = { results?: T[]; meta?: D1Meta };
type D1Prepared = {
  bind(...values: unknown[]): D1Prepared;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
  run(): Promise<unknown>;
};
type RepairD1 = {
  prepare(sql: string): D1Prepared;
  batch?: (statements: D1Prepared[]) => Promise<unknown>;
};
type R2Head = {
  key?: string;
  size?: number;
  customMetadata?: Record<string, string>;
};
type R2Object = R2Head & {
  body?: ReadableStream<Uint8Array> | null;
  arrayBuffer?: () => Promise<ArrayBuffer>;
};
type RepairR2 = {
  get(key: string): Promise<R2Object | null>;
  head(key: string): Promise<R2Head | null>;
};

export type RecoveryProvenanceRuntime = {
  DB?: RepairD1;
  RAW_BUCKET?: RepairR2;
};

export type ProvenanceQuery = {
  start: string;
  end: string;
  cursor: string | null;
  limit: number;
};

export type ProvenanceIssue = {
  code: string;
  asOf: string | null;
  domain: string | null;
  detail: string;
};

export type ProvenanceStageEvidence = {
  asOf: string;
  domain: string;
  dataset: string;
  sourceId: string;
  raw: "READY" | "MISSING" | "HASH_MISSING" | "HASH_MISMATCH" | "UNKNOWN";
  normalized: "READY" | "MISSING" | "HASH_MISSING" | "HASH_MISMATCH" | "UNKNOWN";
  model: "UNKNOWN";
  dashboard: "UNKNOWN";
  etf_penetration: "UNKNOWN";
  date_visible: "UNKNOWN";
};

export type ProvenanceReadinessCandidate = {
  targetDate: string;
  sourceId: string;
  required: boolean;
  observedAt: string;
  deadlineAt: string;
  observedDates: string[];
  watermark: Record<string, unknown>;
  reason: "official_source_archived";
  runId: string;
  evidence: Record<string, unknown>;
};

export type ProvenancePointerCandidate = {
  sourceId: string;
  dataset: string;
  tradeDate: string;
  fileName: string;
  revisionId: string;
  objectKey: string;
  sha256: string;
  contentEncoding: "gzip" | null;
  rowCount: number;
  observedAt: string;
};

export type ProvenanceManifestResult = {
  asOf: string;
  manifestKey: string;
  manifestSha256: string | null;
  status: "READY" | "INCOMPLETE";
  artifactCount: number;
  readinessCount: number;
  pointerCount: number;
  issues: ProvenanceIssue[];
};

export type RecoveryProvenancePlan = {
  schemaVersion: typeof RECOVERY_PROVENANCE_SCHEMA_VERSION;
  contract: typeof RECOVERY_PROVENANCE_CONTRACT;
  scope: { start: string; end: string };
  request: ProvenanceQuery;
  status: "READY_TO_APPLY_METADATA_ONLY" | "INCOMPLETE" | "NO_EVIDENCE";
  planHash: string;
  planStatus: "NOT_APPROVED";
  reconciliationStatus: "PENDING";
  executionBlocked: true;
  budgetStatus: "unknown";
  d1: { queryCount: number; rowsRead: number | null; rowsWritten: number | null; rowsReadKnown: boolean; rowsWrittenKnown: boolean };
  r2: { gets: number; heads: number; bytesRead: number; listCalls: 0; rawBodiesRead: 0; normalizedBodiesRead: 0 };
  hasMore: boolean;
  nextCursor: string | null;
  manifests: ProvenanceManifestResult[];
  stageEvidence: ProvenanceStageEvidence[];
  recoveryPlan: Array<{ asOf: string; domain: string; action: "METADATA_REPAIR"; status: "READY" | "BLOCKED"; reason: string }>;
  sourceUnavailable: Array<{ asOf: string; domain: string; reason: string }>;
  issues: ProvenanceIssue[];
  readinessRows: ProvenanceReadinessCandidate[];
  pointerRows: ProvenancePointerCandidate[];
};

type HistoricalRow = {
  as_of: string;
  observed_at: string | null;
  snapshot_sha256: string | null;
  r2_object_key: string | null;
  manifest_key: string | null;
};

type ManifestArtifact = {
  releaseId: string;
  sourceId: string;
  dataset: string;
  requestedDate: string;
  tradeDates: string[];
  observedAt: string;
  rawKey: string;
  metadataKey: string;
  normalizedKeys: string[];
  stateKey?: string;
};

type ValidatedArtifact = {
  artifact: ManifestArtifact;
  rawSha256: string;
  normalized: Array<{ key: string; fileName: string; sha256: string; rowCount: number; contentEncoding: "gzip" | null; revisionId?: string }>;
  observedAt: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isCalendarDate(value: unknown): boolean {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function isInstant(value: unknown): value is string {
  return typeof value === "string" && ISO_INSTANT.test(value) && !Number.isNaN(Date.parse(value));
}

function canonicalDate(value: string): string | null {
  if (isCalendarDate(value)) return value;
  if (/^\d{8}$/.test(value)) {
    const candidate = `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
    return isCalendarDate(candidate) ? candidate : null;
  }
  return null;
}

function canonicalObservedAt(value: string, fallback: string): string | null {
  if (isInstant(value)) return new Date(value).toISOString();
  const parsed = Date.parse(value);
  if (!Number.isNaN(parsed)) return new Date(parsed).toISOString();
  return isInstant(fallback) ? new Date(fallback).toISOString() : null;
}

function sourceReadinessId(sourceId: string, dataset: string): string | null {
  if (dataset === "etf_scale") return `${sourceId}_etf`;
  if (dataset === "etf_nav") return "sse_etf_nav";
  if (dataset === "etf_pcf") return "sse_etf_pcf";
  if (dataset === "margin_market_daily") return `${sourceId}_margin`;
  if (dataset === "stock_connect_daily") return "stock_connect_summary";
  return null;
}

function pointerDataset(dataset: string): "etf_scale" | "etf_pcf" | null {
  return dataset === "etf_scale" || dataset === "etf_pcf" ? dataset : null;
}

function sourceDomain(sourceId: string, dataset: string): string | null {
  if (dataset === "etf_scale" && (sourceId === "sse" || sourceId === "szse")) return `${sourceId}_etf`;
  if ((dataset === "etf_nav" || dataset === "etf_pcf") && sourceId === "sse") return "sse_etf";
  if (dataset === "margin_market_daily" && ["sse", "szse", "bse"].includes(sourceId)) return `${sourceId}_margin`;
  if (dataset === "stock_connect_daily" && sourceId === "hkex") return "hkex_connect";
  return null;
}

function rawPartition(value: string): string | null {
  if (value === "current") return value;
  return canonicalDate(value) ? value : null;
}

function repairManifestKey(value: string, asOf: string): boolean {
  const match = HISTORY_MANIFEST_KEY.exec(value);
  return Boolean(match && match[2] === asOf && !value.includes("..") && !value.includes("//"));
}

function parseQueryValue(value: string | null, fallback: string): string {
  return value === null || value === "" ? fallback : value;
}

export function parseProvenanceQuery(params: URLSearchParams): ProvenanceQuery {
  const start = parseQueryValue(params.get("start"), RECOVERY_PROVENANCE_SCOPE_START);
  const end = parseQueryValue(params.get("end"), RECOVERY_PROVENANCE_SCOPE_END);
  const cursor = params.get("cursor");
  const rawLimit = params.get("limit");
  const limit = rawLimit === null || rawLimit === "" ? DEFAULT_PROVENANCE_LIMIT : Number(rawLimit);
  if (!isCalendarDate(start) || start < RECOVERY_PROVENANCE_SCOPE_START || start > RECOVERY_PROVENANCE_SCOPE_END) {
    throw new Error("invalid_provenance_start");
  }
  if (!isCalendarDate(end) || end < start || end > RECOVERY_PROVENANCE_SCOPE_END) {
    throw new Error("invalid_provenance_end");
  }
  if (cursor !== null && !isCalendarDate(cursor)) throw new Error("invalid_provenance_cursor");
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_PROVENANCE_LIMIT) throw new Error("invalid_provenance_limit");
  return { start, end, cursor, limit };
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as BufferSource);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right));
  return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
}

async function buildPlanHash(value: Omit<RecoveryProvenancePlan, "planHash">): Promise<string> {
  // The byte hash of a JSON manifest is retained as evidence, but JSON member
  // or artifact ordering is not a business fact. Exclude that transport-level
  // digest from the canonical plan hash so a reordered equivalent fixture
  // cannot create a different repair plan or defeat an idempotent apply.
  const stripTransportDigests = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map((entry) => stripTransportDigests(entry));
    if (!item || typeof item !== "object") return item;
    const output: Record<string, unknown> = {};
    for (const [childKey, childValue] of Object.entries(item as Record<string, unknown>)) {
      output[childKey] = childKey === "manifestSha256" || childKey === "archiveManifestSha256"
        ? null
        : stripTransportDigests(childValue);
    }
    return output;
  };
  const projection = stripTransportDigests(value) as Record<string, unknown>;
  return sha256Hex(new TextEncoder().encode(canonicalJson(projection)));
}

async function readManifest(bucket: RepairR2, key: string, usage: { gets: number; bytesRead: number }): Promise<{ value: Record<string, unknown> | null; sha256: string | null; issue: string | null }> {
  usage.gets += 1;
  const object = await bucket.get(key);
  if (!object) return { value: null, sha256: null, issue: "MANIFEST_MISSING" };
  if (typeof object.size === "number" && (!Number.isSafeInteger(object.size) || object.size < 0 || object.size > MAX_MANIFEST_BYTES)) {
    return { value: null, sha256: null, issue: "MANIFEST_TOO_LARGE" };
  }
  let bytes: Uint8Array;
  try {
    if (object.arrayBuffer) bytes = new Uint8Array(await object.arrayBuffer());
    else if (object.body) bytes = new Uint8Array(await new Response(object.body).arrayBuffer());
    else return { value: null, sha256: null, issue: "MANIFEST_BODY_MISSING" };
  } catch {
    return { value: null, sha256: null, issue: "MANIFEST_READ_FAILED" };
  }
  usage.bytesRead += bytes.byteLength;
  if (bytes.byteLength > MAX_MANIFEST_BYTES) return { value: null, sha256: null, issue: "MANIFEST_TOO_LARGE" };
  const digest = await sha256Hex(bytes);
  try {
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return isRecord(parsed) ? { value: parsed, sha256: digest, issue: null } : { value: null, sha256: digest, issue: "MANIFEST_INVALID" };
  } catch {
    return { value: null, sha256: digest, issue: "MANIFEST_INVALID" };
  }
}

function artifactFromValue(value: unknown): ManifestArtifact | null {
  if (!isRecord(value)) return null;
  const releaseId = typeof value.releaseId === "string" ? value.releaseId : "";
  const sourceId = typeof value.sourceId === "string" ? value.sourceId : "";
  const dataset = typeof value.dataset === "string" ? value.dataset : "";
  const requestedDate = typeof value.requestedDate === "string" ? value.requestedDate : "";
  const observedAt = typeof value.observedAt === "string" ? value.observedAt : "";
  const rawKey = typeof value.rawKey === "string" ? value.rawKey : "";
  const metadataKey = typeof value.metadataKey === "string" ? value.metadataKey : "";
  const tradeDates = Array.isArray(value.tradeDates) ? value.tradeDates.filter((item): item is string => typeof item === "string") : [];
  const normalizedKeys = Array.isArray(value.normalizedKeys) ? value.normalizedKeys.filter((item): item is string => typeof item === "string") : [];
  const stateKey = typeof value.stateKey === "string" ? value.stateKey : undefined;
  if (!releaseId || !SAFE_SEGMENT.test(releaseId) || !SOURCES.has(sourceId) || !DATASETS.has(dataset) || !sourceDomain(sourceId, dataset) || !rawPartition(requestedDate) || !isInstant(observedAt) || !rawKey || !metadataKey || !tradeDates.length || normalizedKeys.length > 2) return null;
  return { releaseId, sourceId, dataset, requestedDate, tradeDates, observedAt, rawKey, metadataKey, normalizedKeys, stateKey };
}

async function validateArtifact(
  bucket: RepairR2,
  asOf: string,
  artifact: ManifestArtifact,
  usage: { heads: number },
  issues: ProvenanceIssue[],
): Promise<ValidatedArtifact | null> {
  const domain = sourceDomain(artifact.sourceId, artifact.dataset);
  const raw = RAW_KEY.exec(artifact.rawKey);
  if (!raw || raw[1] !== artifact.sourceId || raw[2] !== artifact.dataset || raw[3] !== artifact.requestedDate || raw[4] !== raw[4].toLowerCase() || artifact.metadataKey !== `${artifact.rawKey.slice(0, -raw[5].length - 1)}.metadata.json`) {
    issues.push({ code: "RAW_KEY_INVALID", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  const dates = [...new Set(artifact.tradeDates.map(canonicalDate).filter((item): item is string => Boolean(item)))].sort();
  if (!dates.length || dates.some((date) => date < RECOVERY_PROVENANCE_SCOPE_START || date > RECOVERY_PROVENANCE_SCOPE_END)) {
    issues.push({ code: "TRADE_DATE_INVALID_OR_OUT_OF_SCOPE", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  if (!artifact.normalizedKeys.length) {
    issues.push({ code: "NORMALIZED_DECLARATION_EMPTY", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  if (dates.some((date) => date !== asOf && artifact.dataset === "etf_scale")) {
    issues.push({ code: "ETF_TRADE_DATE_MISMATCH", asOf, domain, detail: `${artifact.releaseId}:${dates.join(",")}` });
    return null;
  }
  if (usage.heads + 2 + artifact.normalizedKeys.length > MAX_R2_HEADS) {
    issues.push({ code: "R2_HEAD_CAP_EXCEEDED", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  usage.heads += 1;
  let rawHead: R2Head | null;
  let metadataHead: R2Head | null;
  try {
    rawHead = await bucket.head(artifact.rawKey);
    usage.heads += 1;
    metadataHead = await bucket.head(artifact.metadataKey);
  } catch {
    issues.push({ code: "R2_HEAD_FAILED", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  const rawMetadata = rawHead?.customMetadata ?? {};
  if (!rawHead || !metadataHead) {
    issues.push({ code: "RAW_OR_METADATA_MISSING", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  if (rawMetadata.sourceId !== artifact.sourceId || rawMetadata.dataset !== artifact.dataset || rawMetadata.requestedDate !== artifact.requestedDate) {
    issues.push({ code: "RAW_METADATA_MISMATCH", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  if (!SHA256.test(rawMetadata.sha256 ?? "") || rawMetadata.sha256 !== raw[4] || metadataHead.customMetadata?.sha256 !== raw[4]) {
    issues.push({ code: "RAW_HASH_MISSING_OR_MISMATCH", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  const normalized: ValidatedArtifact["normalized"] = [];
  for (const key of artifact.normalizedKeys) {
    const match = NORMALIZED_KEY.exec(key);
    if (!match || match[1] !== artifact.sourceId || match[2] !== artifact.dataset || match[3] !== artifact.requestedDate) {
      issues.push({ code: "NORMALIZED_KEY_INVALID", asOf, domain, detail: key });
      return null;
    }
    usage.heads += 1;
    let head: R2Head | null;
    try {
      head = await bucket.head(key);
    } catch {
      issues.push({ code: "R2_HEAD_FAILED", asOf, domain, detail: key });
      return null;
    }
    const metadata = head?.customMetadata ?? {};
    if (!head) {
      issues.push({ code: "NORMALIZED_MISSING", asOf, domain, detail: key });
      return null;
    }
    const sha256 = metadata.sha256 ?? "";
    const contentAddressed = metadata.normalizedKeyVersion === "normalized-source-wire-sha256-v1";
    if (contentAddressed
      ? artifact.dataset !== "etf_scale" || metadata.rawSha256 !== raw[4]
        || !SHA256.test(metadata.wireSha256 ?? "")
        || await sha256Hex(new TextEncoder().encode(`${raw[4]}:${metadata.wireSha256}`)) !== match[4]
        || (!key.endsWith(".gz") && sha256 !== metadata.wireSha256)
      : metadata.normalizedKeyVersion !== undefined || match[4] !== raw[4]) {
      issues.push({ code: "NORMALIZED_KEY_INVALID", asOf, domain, detail: key });
      return null;
    }
    const rowCount = Number(metadata.rowCount);
    if (!SHA256.test(sha256)) {
      issues.push({ code: "NORMALIZED_HASH_MISSING", asOf, domain, detail: key });
      return null;
    }
    if (metadata.sourceId !== artifact.sourceId || metadata.dataset !== artifact.dataset) {
      issues.push({ code: "NORMALIZED_METADATA_MISMATCH", asOf, domain, detail: key });
      return null;
    }
    if (!Number.isSafeInteger(rowCount) || rowCount < 0) {
      issues.push({ code: "NORMALIZED_ROW_COUNT_INVALID", asOf, domain, detail: key });
      return null;
    }
    normalized.push({ key, fileName: match[5].replace(/\.gz$/, ""), sha256, rowCount, contentEncoding: key.endsWith(".gz") ? "gzip" : null,
      ...(contentAddressed ? { revisionId: match[4] } : {}) });
  }
  const observedAt = canonicalObservedAt(artifact.observedAt, `${asOf}T00:00:00.000Z`);
  if (!observedAt) {
    issues.push({ code: "OBSERVED_AT_INVALID", asOf, domain, detail: artifact.releaseId });
    return null;
  }
  return { artifact: { ...artifact, tradeDates: dates }, rawSha256: raw[4], normalized, observedAt };
}

function appendCandidates(
  validated: ValidatedArtifact,
  manifestKey: string,
  manifestSha256: string,
  readinessRows: Map<string, ProvenanceReadinessCandidate>,
  pointerRows: Map<string, ProvenancePointerCandidate>,
  stageEvidence: ProvenanceStageEvidence[],
): void {
  const { artifact, normalized, observedAt, rawSha256 } = validated;
  const domain = sourceDomain(artifact.sourceId, artifact.dataset);
  if (!domain) return;
  const readinessSource = sourceReadinessId(artifact.sourceId, artifact.dataset);
  const dates = artifact.tradeDates;
  if (readinessSource) {
    for (const targetDate of dates) {
      const key = `${targetDate}:${readinessSource}`;
      const evidence = {
        archiveVerified: true,
        archiveManifestKey: manifestKey,
        archiveManifestSha256: manifestSha256,
        objectKey: artifact.rawKey,
        rawObjectKey: artifact.rawKey,
        sha256: rawSha256,
        rawSha256,
        normalizedKeys: normalized.map((item) => item.key),
        normalizedSha256: normalized.map((item) => ({ name: item.fileName, sha256: item.sha256, rowCount: item.rowCount })),
        sourceBusinessDate: targetDate,
        sourceFingerprint: rawSha256,
        evidenceSource: "recovery-provenance-repair",
        evidenceCapturedAt: observedAt,
      };
      const existing = readinessRows.get(key);
      if (!existing || observedAt >= existing.observedAt) {
        readinessRows.set(key, {
          targetDate,
          sourceId: readinessSource,
          required: REQUIRED_DATASETS.has(artifact.dataset),
          observedAt,
          deadlineAt: observedAt,
          observedDates: dates,
          watermark: { observedTradeDate: targetDate, archiveManifestKey: manifestKey },
          reason: "official_source_archived",
          runId: `r0d-provenance-repair:${targetDate}:${rawSha256.slice(0, 16)}`,
          evidence,
        });
      }
    }
  }
  const pointer = pointerDataset(artifact.dataset);
  if (pointer) {
    for (const file of normalized) {
      for (const tradeDate of dates) {
        const key = `${artifact.sourceId}:${pointer}:${tradeDate}:${file.fileName}`;
        const candidate = {
          sourceId: artifact.sourceId,
          dataset: pointer,
          tradeDate,
          fileName: file.fileName,
          revisionId: file.revisionId ?? artifact.releaseId,
          objectKey: file.key,
          sha256: file.sha256,
          contentEncoding: file.contentEncoding,
          rowCount: file.rowCount,
          observedAt,
        } satisfies ProvenancePointerCandidate;
        const existing = pointerRows.get(key);
        if (!existing || observedAt >= existing.observedAt) pointerRows.set(key, candidate);
      }
    }
  }
  for (const asOf of dates) {
    stageEvidence.push({ asOf, domain, dataset: artifact.dataset, sourceId: artifact.sourceId, raw: "READY", normalized: normalized.length ? "READY" : "MISSING", model: "UNKNOWN", dashboard: "UNKNOWN", etf_penetration: "UNKNOWN", date_visible: "UNKNOWN" });
  }
}

function issue(code: string, asOf: string | null, detail: string, domain: string | null = null): ProvenanceIssue {
  return { code, asOf, domain, detail };
}

async function readHistoricalRows(runtime: RecoveryProvenanceRuntime, query: ProvenanceQuery): Promise<{ rows: HistoricalRow[]; hasMore: boolean; nextCursor: string | null; rowsRead: number | null; rowsWritten: number | null }> {
  if (!runtime.DB) throw new Error("database_unavailable");
  const result = await runtime.DB.prepare(
    `SELECT as_of, observed_at, snapshot_sha256, r2_object_key, manifest_key
       FROM historical_snapshot
      WHERE as_of >= ? AND as_of <= ? AND as_of > ?
      ORDER BY as_of ASC
      LIMIT ?`,
  ).bind(query.start, query.end, query.cursor ?? "", query.limit + 1).all<HistoricalRow>();
  const rows = result.results ?? [];
  const selected = rows.slice(0, query.limit).filter((row) => isCalendarDate(row.as_of));
  const last = selected[selected.length - 1]?.as_of ?? null;
  const rowsRead = typeof result.meta?.rows_read === "number" && Number.isSafeInteger(result.meta.rows_read) ? result.meta.rows_read : null;
  const rowsWritten = typeof result.meta?.rows_written === "number" && Number.isSafeInteger(result.meta.rows_written) ? result.meta.rows_written : null;
  return { rows: selected, hasMore: rows.length > query.limit, nextCursor: rows.length > query.limit ? last : null, rowsRead, rowsWritten };
}

export async function buildRecoveryProvenancePlan(runtime: RecoveryProvenanceRuntime, query: ProvenanceQuery): Promise<RecoveryProvenancePlan> {
  const base: Omit<RecoveryProvenancePlan, "planHash"> = {
    schemaVersion: RECOVERY_PROVENANCE_SCHEMA_VERSION,
    contract: RECOVERY_PROVENANCE_CONTRACT,
    scope: { start: RECOVERY_PROVENANCE_SCOPE_START, end: RECOVERY_PROVENANCE_SCOPE_END },
    request: query,
    status: "NO_EVIDENCE",
    planStatus: "NOT_APPROVED" as const,
    reconciliationStatus: "PENDING" as const,
    executionBlocked: true as const,
    budgetStatus: "unknown" as const,
    d1: { queryCount: 0, rowsRead: null, rowsWritten: null, rowsReadKnown: false, rowsWrittenKnown: false },
    r2: { gets: 0, heads: 0, bytesRead: 0, listCalls: 0, rawBodiesRead: 0, normalizedBodiesRead: 0 },
    hasMore: false,
    nextCursor: null as string | null,
    manifests: [] as ProvenanceManifestResult[],
    stageEvidence: [] as ProvenanceStageEvidence[],
    recoveryPlan: [] as RecoveryProvenancePlan["recoveryPlan"],
    sourceUnavailable: [] as RecoveryProvenancePlan["sourceUnavailable"],
    issues: [] as ProvenanceIssue[],
    readinessRows: [] as ProvenanceReadinessCandidate[],
    pointerRows: [] as ProvenancePointerCandidate[],
  };
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    base.issues.push(issue("STORAGE_UNAVAILABLE", null, "D1 or R2 binding is unavailable"));
    return { ...base, planHash: await buildPlanHash(base) };
  }
  let historical;
  try {
    historical = await readHistoricalRows(runtime, query);
    base.d1.queryCount = 1;
    base.d1.rowsRead = historical.rowsRead;
    base.d1.rowsWritten = historical.rowsWritten;
    base.d1.rowsReadKnown = historical.rowsRead !== null;
    base.d1.rowsWrittenKnown = historical.rowsWritten !== null;
    base.hasMore = historical.hasMore;
    base.nextCursor = historical.nextCursor;
  } catch {
    base.issues.push(issue("D1_READ_FAILED", null, "historical_snapshot query failed"));
    return { ...base, status: "INCOMPLETE", planHash: await buildPlanHash(base) };
  }
  if (base.d1.rowsRead !== null && base.d1.rowsRead > MAX_D1_ROWS_READ) base.issues.push(issue("D1_ROW_CAP_EXCEEDED", null, String(base.d1.rowsRead)));
  if (!base.d1.rowsReadKnown || !base.d1.rowsWrittenKnown) base.issues.push(issue("D1_TELEMETRY_UNKNOWN", null, "D1 meta rows_read/rows_written is unavailable"));
  if (base.d1.rowsWritten !== null && base.d1.rowsWritten !== 0) base.issues.push(issue("D1_READONLY_VIOLATION", null, "read query reported rows_written > 0"));
  if (!historical.rows.length) base.issues.push(issue("NO_HISTORICAL_SNAPSHOTS", null, "no rows in the requested bounded page"));

  const readinessRows = new Map<string, ProvenanceReadinessCandidate>();
  const pointerRows = new Map<string, ProvenancePointerCandidate>();
  for (const row of historical.rows) {
    const asOf = row.as_of;
    const manifestKey = row.manifest_key ?? "";
    const manifestResult: ProvenanceManifestResult = { asOf, manifestKey, manifestSha256: null, status: "INCOMPLETE", artifactCount: 0, readinessCount: 0, pointerCount: 0, issues: [] };
    if (!manifestKey || !repairManifestKey(manifestKey, asOf)) {
      const item = issue("ARCHIVE_PROVENANCE_INCOMPLETE", asOf, "historical_snapshot has no valid published history manifest key");
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    let manifestRead: { value: Record<string, unknown> | null; sha256: string | null; issue: string | null };
    try {
      manifestRead = await readManifest(runtime.RAW_BUCKET, manifestKey, base.r2);
    } catch {
      manifestRead = { value: null, sha256: null, issue: "MANIFEST_READ_FAILED" };
    }
    if (manifestRead.sha256) manifestResult.manifestSha256 = manifestRead.sha256;
    if (!manifestRead.value) {
      const item = issue(manifestRead.issue ?? "MANIFEST_INVALID", asOf, manifestKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    const manifest = manifestRead.value;
    if (manifest.asOf !== asOf || typeof manifest.runId !== "string" || !isInstant(manifest.finishedAt)) {
      const item = issue("MANIFEST_IDENTITY_INVALID", asOf, manifestKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    if (typeof manifest.snapshotSha256 !== "string" || !SHA256.test(manifest.snapshotSha256) || !SHA256.test(row.snapshot_sha256 ?? "") || manifest.snapshotSha256 !== row.snapshot_sha256) {
      const item = issue("SNAPSHOT_HASH_MISSING_OR_MISMATCH", asOf, manifestKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    if (typeof manifest.snapshotKey !== "string" || !HISTORY_SNAPSHOT_KEY.test(manifest.snapshotKey) || !manifest.snapshotKey.startsWith(`market-data/history/${asOf}/`) || row.r2_object_key !== manifest.snapshotKey) {
      const item = issue("SNAPSHOT_KEY_INVALID", asOf, manifestKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    if (base.r2.heads + 1 > MAX_R2_HEADS) {
      const item = issue("R2_HEAD_CAP_EXCEEDED", asOf, manifest.snapshotKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    base.r2.heads += 1;
    let snapshotHead: R2Head | null;
    try {
      snapshotHead = await runtime.RAW_BUCKET.head(manifest.snapshotKey);
    } catch {
      const item = issue("R2_HEAD_FAILED", asOf, manifest.snapshotKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    if (!snapshotHead || snapshotHead.customMetadata?.sha256 !== manifest.snapshotSha256) {
      const item = issue("SNAPSHOT_OBJECT_MISSING_OR_HASH_MISMATCH", asOf, manifest.snapshotKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    if (!Array.isArray(manifest.artifacts) || manifest.artifacts.length === 0 || manifest.artifacts.length > 16) {
      const item = issue("MANIFEST_ARTIFACTS_INVALID", asOf, manifestKey);
      manifestResult.issues.push(item);
      base.issues.push(item);
      base.manifests.push(manifestResult);
      continue;
    }
    let manifestValid = true;
    const beforeReadiness = readinessRows.size;
    const beforePointers = pointerRows.size;
    const declaredArtifacts = [...manifest.artifacts].sort((left, right) => canonicalJson(left).localeCompare(canonicalJson(right)));
    for (const rawArtifact of declaredArtifacts) {
      const artifact = artifactFromValue(rawArtifact);
      if (!artifact) {
        manifestValid = false;
        const item = issue("ARTIFACT_DECLARATION_INVALID", asOf, manifestKey);
        manifestResult.issues.push(item);
        base.issues.push(item);
        continue;
      }
      const validated = await validateArtifact(runtime.RAW_BUCKET, asOf, artifact, base.r2, base.issues);
      if (!validated) {
        manifestValid = false;
        continue;
      }
      appendCandidates(validated, manifestKey, manifestRead.sha256 ?? "", readinessRows, pointerRows, base.stageEvidence);
    }
    manifestResult.artifactCount = manifest.artifacts.length;
    manifestResult.readinessCount = readinessRows.size - beforeReadiness;
    manifestResult.pointerCount = pointerRows.size - beforePointers;
    if (!manifestValid) {
      const item = issue("ARCHIVE_PROVENANCE_INCOMPLETE", asOf, "one or more manifest artifacts failed exact R2 validation");
      manifestResult.issues.push(item);
      base.issues.push(item);
    } else {
      manifestResult.status = "READY";
    }
    base.manifests.push(manifestResult);
  }
  base.readinessRows = [...readinessRows.values()].sort((left, right) => `${left.targetDate}:${left.sourceId}`.localeCompare(`${right.targetDate}:${right.sourceId}`));
  base.pointerRows = [...pointerRows.values()].sort((left, right) => `${left.tradeDate}:${left.sourceId}:${left.dataset}:${left.fileName}`.localeCompare(`${right.tradeDate}:${right.sourceId}:${right.dataset}:${right.fileName}`));
  const pageDates = historical.rows.map((row) => row.as_of).sort();
  const pageDomains = ["sse_etf", "szse_etf", "sse_margin", "szse_margin", "bse_margin", "hkex_connect"];
  const verifiedStageEvidence = base.stageEvidence;
  base.stageEvidence = [];
  for (const asOf of pageDates) {
    for (const domain of pageDomains) {
      const entries = verifiedStageEvidence.filter((item) => item.asOf === asOf && item.domain === domain);
      const combine = (field: "raw" | "normalized"): ProvenanceStageEvidence[typeof field] => {
        const values = entries.map((item) => item[field]);
        if (values.includes("READY")) return "READY";
        if (values.includes("HASH_MISMATCH")) return "HASH_MISMATCH";
        if (values.includes("HASH_MISSING")) return "HASH_MISSING";
        if (values.includes("MISSING")) return "MISSING";
        return "UNKNOWN";
      };
      base.stageEvidence.push({
        asOf,
        domain,
        dataset: entries.length ? [...new Set(entries.map((item) => item.dataset))].sort().join(",") : "__none__",
        sourceId: entries.length ? [...new Set(entries.map((item) => item.sourceId))].sort().join(",") : "__none__",
        raw: combine("raw"),
        normalized: combine("normalized"),
        model: "UNKNOWN",
        dashboard: "UNKNOWN",
        etf_penetration: "UNKNOWN",
        date_visible: "UNKNOWN",
      });
    }
  }
  base.stageEvidence.sort((left, right) => `${left.asOf}:${left.domain}:${left.dataset}:${left.sourceId}`.localeCompare(`${right.asOf}:${right.domain}:${right.dataset}:${right.sourceId}`));
  for (const manifest of base.manifests) manifest.issues.sort((left, right) => canonicalJson(left).localeCompare(canonicalJson(right)));
  base.manifests.sort((left, right) => left.asOf.localeCompare(right.asOf));
  const statementCount = base.readinessRows.length + base.pointerRows.length;
  if (statementCount > MAX_REPAIR_STATEMENTS) base.issues.push(issue("WRITE_CAP_EXCEEDED", null, String(statementCount)));
  const hasReadyManifest = base.manifests.some((item) => item.status === "READY");
  base.status = base.issues.length ? "INCOMPLETE" : hasReadyManifest ? "READY_TO_APPLY_METADATA_ONLY" : "NO_EVIDENCE";
  for (const asOf of pageDates) {
    for (const domain of ["sse_etf", "szse_etf", "sse_margin", "szse_margin", "bse_margin", "hkex_connect"]) {
      const covered = base.stageEvidence.some((item) => item.asOf === asOf && item.domain === domain && item.dataset !== "__none__");
      base.recoveryPlan.push({ asOf, domain, action: "METADATA_REPAIR", status: covered && !base.issues.length ? "READY" : "BLOCKED", reason: covered ? (base.issues.length ? "page_has_unresolved_issues" : "exact_archive_receipts_verified") : "no_verified_archive_artifact" });
    }
  }
  base.issues.sort((left, right) => canonicalJson(left).localeCompare(canonicalJson(right)));
  base.recoveryPlan.sort((left, right) => `${left.asOf}:${left.domain}`.localeCompare(`${right.asOf}:${right.domain}`));
  const withoutHash = { ...base };
  return { ...base, planHash: await buildPlanHash(withoutHash) };
}

export async function applyRecoveryProvenancePlan(runtime: RecoveryProvenanceRuntime, plan: RecoveryProvenancePlan): Promise<{ readinessRowsWritten: number; pointerRowsWritten: number; d1RowsWritten: number }> {
  if (!runtime.DB) throw new Error("database_unavailable");
  if (plan.status !== "READY_TO_APPLY_METADATA_ONLY" || plan.issues.length || plan.budgetStatus !== "unknown") throw new Error("plan_not_applyable");
  const now = new Date().toISOString();
  const statements: D1Prepared[] = [];
  for (const row of plan.pointerRows) {
    statements.push(runtime.DB.prepare(
      `INSERT INTO history_artifact_pointer (
         source_id, dataset, trade_date, file_name, revision_id,
         object_key, sha256, content_encoding, row_count, observed_at,
         status, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?)
       ON CONFLICT(source_id, dataset, trade_date, file_name) DO UPDATE SET
         revision_id = excluded.revision_id,
         object_key = excluded.object_key,
         sha256 = excluded.sha256,
         content_encoding = excluded.content_encoding,
         row_count = excluded.row_count,
         observed_at = excluded.observed_at,
         status = excluded.status,
         updated_at = excluded.updated_at
       WHERE excluded.observed_at >= history_artifact_pointer.observed_at`,
    ).bind(row.sourceId, row.dataset, row.tradeDate, row.fileName, row.revisionId, row.objectKey, row.sha256, row.contentEncoding, row.rowCount, row.observedAt, now));
  }
  for (const row of plan.readinessRows) {
    statements.push(runtime.DB.prepare(
      `INSERT INTO source_readiness (
         target_date, source_id, status, overall_status, required,
         observed_at, next_check_at, deadline_at, deadline_exceeded, attempt,
         observed_dates_json, watermark_json, reason, run_id, evidence_json, updated_at
       ) VALUES (?, ?, 'ready', 'ready', ?, ?, NULL, ?, 0, 1, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(target_date, source_id) DO UPDATE SET
         status = excluded.status,
         overall_status = excluded.overall_status,
         required = excluded.required,
         observed_at = excluded.observed_at,
         next_check_at = excluded.next_check_at,
         deadline_at = excluded.deadline_at,
         deadline_exceeded = excluded.deadline_exceeded,
         attempt = MAX(source_readiness.attempt, excluded.attempt),
         observed_dates_json = excluded.observed_dates_json,
         watermark_json = excluded.watermark_json,
         reason = excluded.reason,
         run_id = excluded.run_id,
         evidence_json = excluded.evidence_json,
         updated_at = excluded.updated_at
       WHERE excluded.observed_at >= source_readiness.observed_at`,
    ).bind(row.targetDate, row.sourceId, row.required ? 1 : 0, row.observedAt, row.deadlineAt, JSON.stringify(row.observedDates), JSON.stringify(row.watermark), row.reason, row.runId, JSON.stringify(row.evidence), now));
  }
  if (statements.length > MAX_REPAIR_STATEMENTS) throw new Error("write_cap_exceeded");
  if (typeof runtime.DB.batch === "function") {
    for (let offset = 0; offset < statements.length; offset += 100) await runtime.DB.batch(statements.slice(offset, offset + 100));
  } else {
    for (const statement of statements) await statement.run();
  }
  return { readinessRowsWritten: plan.readinessRows.length, pointerRowsWritten: plan.pointerRows.length, d1RowsWritten: statements.length };
}
