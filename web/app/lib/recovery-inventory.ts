import {
  hasCompleteReadModelManifest,
  isReadModelManifest,
  READ_MODEL_SLOTS,
  SHA256,
  type ReadModelManifest,
  type ReadModelSlot,
} from "./read-model-contract.ts";
import { PIPELINE_WINDOWS } from "./pipeline-window.ts";

/**
 * Bounded, read-only evidence surface for the controlled recovery auditor.
 *
 * This module deliberately deals in metadata only.  It never calls R2
 * `get()` or `list()`, never writes D1/R2, and never mutates a release pointer.
 * A later evidence exporter may consume this contract, but this endpoint must
 * remain safe to call from a manually-triggered inventory run.
 */

export const RECOVERY_INVENTORY_SCHEMA_VERSION = 1 as const;
export const RECOVERY_INVENTORY_CONTRACT = "recovery-inventory-v1" as const;
export const DEFAULT_INVENTORY_LIMIT = 50;
export const MAX_INVENTORY_LIMIT = 100;
export const MAX_DATE_SPAN_DAYS = 1_100;
export const MAX_D1_ROWS_READ = 500_000;
/** Cloudflare D1 rejects a prepared statement with more than 100 binds. */
export const MAX_D1_BOUND_PARAMETERS = 100;
export const MAX_D1_QUERIES = 12;
export const MAX_R2_HEADS = 64;
export const MAX_RELEASE_ROWS = 600;
export const MAX_POINTER_ROWS = 1_000;
export const MAX_SOURCE_READINESS_ROWS = 500;

export const INVENTORY_DOMAINS = [
  { id: "sse_etf", market: "SSE" },
  { id: "szse_etf", market: "SZSE" },
  { id: "sse_margin", market: "SSE" },
  { id: "szse_margin", market: "SZSE" },
  { id: "bse_margin", market: "BSE" },
  { id: "hkex_connect", market: null },
] as const;

export const INVENTORY_STAGES = [
  "raw",
  "normalized",
  "model",
  "pcf_exact",
  "dashboard",
  "etf_penetration",
  "date_visible",
] as const;

export type InventoryStage = (typeof INVENTORY_STAGES)[number];
export type InventoryDomain = (typeof INVENTORY_DOMAINS)[number]["id"];
export type InventoryStageStatus =
  | "READY"
  | "MISSING"
  | "SOURCE_UNAVAILABLE"
  | "UNKNOWN"
  | "INCOMPLETE"
  | "HASH_MISSING"
  | "HASH_MISMATCH";

export type InventoryQuery = {
  start: string;
  end: string;
  cursor: string | null;
  limit: number;
};

export type InventoryUsage = {
  d1: {
    queryCount: number;
    rowsRead: number | null;
    rowsWritten: number | null;
    rowsReadKnown: boolean;
    rowsWrittenKnown: boolean;
  };
  r2: {
    classA: number;
    classB: number;
    headCount: number;
    headLimit: number;
  };
  truncated: boolean;
};

type D1Meta = {
  rows_read?: unknown;
  rows_written?: unknown;
};

type D1Result<T> = {
  results?: T[];
  meta?: D1Meta;
};

type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
};

type ReadOnlyD1 = {
  prepare(sql: string): D1Statement;
};

type R2Head = {
  key: string;
  size: number;
  customMetadata?: Record<string, string>;
};

type ReadOnlyR2 = {
  head(key: string): Promise<R2Head | null>;
};

export type RecoveryInventoryRuntime = {
  DB?: ReadOnlyD1;
  RAW_BUCKET?: ReadOnlyR2;
};

/** Keep the existing Bearer INGEST_API_KEY contract; this is not a second auth system. */
export function isRecoveryInventoryAuthorized(supplied: string, expected: string): boolean {
  if (!expected || !supplied || supplied.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < supplied.length; index += 1) {
    difference |= supplied.charCodeAt(index) ^ expected.charCodeAt(index);
  }
  return difference === 0;
}

type DateRow = { data_date: string };
type HistoricalRow = {
  as_of: string;
  observed_at: string | null;
  snapshot_sha256: string | null;
  r2_object_key: string | null;
  manifest_key: string | null;
  quality_pass: number;
  quality_warn: number;
  quality_fail: number;
};
type DashboardRow = {
  as_of: string;
  observed_at: string | null;
  published_at: string | null;
  r2_object_key: string | null;
  quality_fail: number;
};
type ReleaseRow = {
  release_id: string;
  as_of: string;
  status: string;
  schema_version: number;
  manifest_sha256: string | null;
  manifest_json: string | null;
  observed_at: string | null;
  created_at: string;
};
type SourceReadinessRow = {
  target_date: string;
  source_id: string;
  status: string;
  required: number;
  observed_at: string | null;
  deadline_at: string | null;
  deadline_exceeded: number;
  observed_dates_json: string | null;
  watermark_json: string | null;
  reason: string | null;
  evidence_json: string | null;
  updated_at: string | null;
};
type PointerRow = {
  source_id: string;
  dataset: string;
  trade_date: string;
  file_name: string;
  revision_id: string;
  object_key: string | null;
  sha256: string | null;
  content_encoding: string | null;
  row_count: number | null;
  observed_at: string | null;
  status: string;
};
type ModelRow = {
  source: "etf" | "margin" | "connect";
  trade_date: string;
  market: string | null;
  row_count: number;
  latest_observed_at: string | null;
};
type MarketHistoryRow = {
  trade_date: string;
  margin_available: number;
  connect_available: number;
  source_snapshot_sha256: string | null;
  observed_at: string | null;
  margin_scope_version: string | null;
};
type PipelineRow = {
  run_key: string;
  pipeline: string;
  window_date: string;
  window_name: string;
  contract_version: string;
  target_date: string | null;
  status: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  source_fingerprint: string | null;
  github_run_id: string | null;
  attempt_count: number;
  last_error_code: string | null;
  updated_at: string | null;
  completed_at: string | null;
  recovery_id: string | null;
};
type RecoveryRow = {
  recovery_id: string;
  run_key: string;
  plan_key: string;
  plan_sha256: string | null;
  scope_sha256: string | null;
  status: string;
  latest_receipt_key: string | null;
  latest_receipt_sha256: string | null;
  updated_at: string | null;
};
type ActiveReleaseRow = {
  slot: string;
  release_id: string;
  as_of: string;
  manifest_sha256: string | null;
  observed_at: string | null;
  activated_at: string | null;
};

export const DATE_CANDIDATES_SQL = `
  WITH dates(data_date) AS (
    SELECT as_of FROM historical_snapshot WHERE as_of BETWEEN ? AND ?
    UNION SELECT as_of FROM release_manifest WHERE as_of BETWEEN ? AND ?
    UNION SELECT as_of FROM dashboard_snapshot WHERE as_of BETWEEN ? AND ?
    UNION SELECT trade_date FROM market_history_daily WHERE trade_date BETWEEN ? AND ?
    UNION SELECT target_date FROM source_readiness WHERE target_date BETWEEN ? AND ?
    UNION SELECT trade_date
      FROM history_artifact_pointer
     WHERE trade_date BETWEEN ? AND ?
       AND dataset IN ('etf_scale', 'etf_pcf')
  )
  SELECT data_date
    FROM dates
   WHERE data_date > ?
   ORDER BY data_date ASC
   LIMIT ?`;

const HISTORICAL_SQL = `
  SELECT as_of, observed_at, snapshot_sha256, r2_object_key, manifest_key,
         quality_pass, quality_warn, quality_fail
    FROM historical_snapshot
   WHERE as_of BETWEEN ? AND ?
   ORDER BY as_of ASC
   LIMIT ?`;

const DASHBOARD_SQL = `
  SELECT as_of, observed_at, published_at, r2_object_key, quality_fail
    FROM dashboard_snapshot
   WHERE as_of BETWEEN ? AND ?
   ORDER BY as_of ASC, published_at DESC
   LIMIT ?`;

const RELEASE_SQL = `
  SELECT release_id, as_of, status, schema_version, manifest_sha256,
         manifest_json, observed_at, created_at
    FROM release_manifest
   WHERE as_of BETWEEN ? AND ?
     AND status IN ('ACTIVE', 'SUPERSEDED', 'STAGED')
   ORDER BY as_of ASC, created_at DESC
   LIMIT ?`;

const SOURCE_READINESS_SQL = `
  SELECT target_date, source_id, status, required, observed_at, deadline_at,
         deadline_exceeded, observed_dates_json, watermark_json, reason,
         evidence_json, updated_at
    FROM source_readiness
   WHERE target_date BETWEEN ? AND ?
   ORDER BY target_date ASC, required DESC, source_id ASC
   LIMIT ?`;

const POINTER_SQL = `
   SELECT source_id, dataset, trade_date, file_name, revision_id, object_key,
          sha256, content_encoding, row_count, observed_at, status
    FROM history_artifact_pointer
   WHERE trade_date BETWEEN ? AND ?
     AND dataset IN ('etf_scale', 'etf_pcf')
   ORDER BY trade_date ASC, source_id ASC, dataset ASC, file_name ASC
   LIMIT ?`;

const MODEL_SQL = `
  SELECT 'etf' AS source, trade_date, market, COUNT(*) AS row_count, MAX(observed_at) AS latest_observed_at
    FROM etf_daily
   WHERE trade_date BETWEEN ? AND ?
   GROUP BY trade_date, market
   UNION ALL
  SELECT 'margin' AS source, trade_date, market, COUNT(*) AS row_count, MAX(observed_at) AS latest_observed_at
    FROM margin_daily
   WHERE trade_date BETWEEN ? AND ?
   GROUP BY trade_date, market
   UNION ALL
  SELECT 'connect' AS source, trade_date, market, COUNT(*) AS row_count, MAX(observed_at) AS latest_observed_at
    FROM connect_daily
   WHERE trade_date BETWEEN ? AND ?
   GROUP BY trade_date, market
   ORDER BY trade_date ASC, market ASC
   LIMIT ?`;

const MARKET_HISTORY_SQL = `
  SELECT trade_date, margin_available, connect_available, source_snapshot_sha256,
         observed_at, margin_scope_version
    FROM market_history_daily
   WHERE trade_date BETWEEN ? AND ?
   ORDER BY trade_date ASC
   LIMIT ?`;

const ACTIVE_RELEASE_SQL = `
  SELECT slot, release_id, as_of, manifest_sha256, observed_at, activated_at
    FROM active_release
   WHERE slot IN ('dashboard', 'etf_penetration')
   LIMIT ?`;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SECRET_KEY_PARTS = ["token", "secret", "password", "cookie", "authorization", "access_key", "api_key"];

function validDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function dateSpanDays(start: string, end: string): number {
  return Math.floor((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000) + 1;
}

function base64UrlEncode(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function base64UrlDecode(value: string): string | null {
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(value)) return null;
  try {
    const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "===".slice((value.length + 3) % 4);
    const binary = atob(padded);
    return new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
  } catch {
    return null;
  }
}

export function encodeInventoryCursor(date: string): string {
  if (!validDate(date)) throw new Error("invalid_cursor_date");
  return base64UrlEncode(JSON.stringify({ v: 1, date }));
}

export function decodeInventoryCursor(value: string): string | null {
  const decoded = base64UrlDecode(value);
  if (!decoded) return null;
  try {
    const parsed: unknown = JSON.parse(decoded);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const candidate = parsed as Record<string, unknown>;
    return candidate.v === 1 && typeof candidate.date === "string" && validDate(candidate.date)
      ? candidate.date
      : null;
  } catch {
    return null;
  }
}

export function parseInventoryQuery(params: URLSearchParams): InventoryQuery {
  const start = params.get("start") ?? "";
  const end = params.get("end") ?? "";
  if (!validDate(start)) throw new Error("invalid_start_date");
  if (!validDate(end)) throw new Error("invalid_end_date");
  if (end < start) throw new Error("invalid_date_range");
  if (dateSpanDays(start, end) > MAX_DATE_SPAN_DAYS) throw new Error("date_range_too_large");

  const rawLimit = params.get("limit");
  const limit = rawLimit === null || rawLimit === "" ? DEFAULT_INVENTORY_LIMIT : Number(rawLimit);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_INVENTORY_LIMIT) {
    throw new Error("invalid_limit");
  }

  const rawCursor = params.get("cursor");
  const cursor = rawCursor ? decodeInventoryCursor(rawCursor) : null;
  if (rawCursor && !cursor) throw new Error("invalid_cursor");
  if (cursor && (cursor < start || cursor >= end)) throw new Error("cursor_out_of_range");
  return { start, end, cursor, limit };
}

function safeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function parseJsonRecord(value: string | null): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function parseJsonValue(value: string | null): unknown {
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function sanitizeEvidence(value: unknown, depth = 0): unknown {
  if (depth > 4 || value === null || typeof value !== "object") return typeof value === "bigint" ? String(value) : value;
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => sanitizeEvidence(item, depth + 1));
  const output: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const lowered = key.toLowerCase();
    if (SECRET_KEY_PARTS.some((part) => lowered.includes(part))) continue;
    output[key] = sanitizeEvidence(child, depth + 1);
  }
  return output;
}

function shaStatus(value: string | null | undefined): "READY" | "HASH_MISSING" {
  return value && SHA256.test(value) ? "READY" : "HASH_MISSING";
}

function safeKey(value: string | null | undefined, prefix: string): boolean {
  return Boolean(
    value && value.length <= 512 && value.startsWith(prefix) &&
      !value.includes("..") && !value.includes("//") && !/[\r\n]/.test(value),
  );
}

function sourceUnavailableProvenance(row: SourceReadinessRow): boolean {
  const evidence = parseJsonRecord(row.evidence_json);
  const fingerprint = evidence.sourceFingerprint ?? evidence.source_fingerprint;
  return Boolean(
    row.source_id && row.target_date && row.reason && row.reason.length > 0 &&
      row.observed_at && Object.keys(evidence).length > 0 &&
      typeof fingerprint === "string" && fingerprint.length > 0,
  );
}

function stageMap(value: InventoryStageStatus = "UNKNOWN"): Record<InventoryStage, InventoryStageStatus> {
  return Object.fromEntries(INVENTORY_STAGES.map((stage) => [stage, value])) as Record<InventoryStage, InventoryStageStatus>;
}

function domainRecord(domain: InventoryDomain, market: string | null) {
  return {
    domain,
    market,
    stageStatus: stageMap(),
    evidence: {
      raw: [] as Record<string, unknown>[],
      normalized: [] as Record<string, unknown>[],
      model: [] as Record<string, unknown>[],
      pcfExact: [] as Record<string, unknown>[],
      dashboard: [] as Record<string, unknown>[],
      etfPenetration: [] as Record<string, unknown>[],
      dateVisible: [] as Record<string, unknown>[],
    },
  };
}

type DateInventory = ReturnType<typeof domainRecord> & { dataDate: string };

function pointerApplies(domain: InventoryDomain, pointer: PointerRow): boolean {
  if (domain === "sse_etf") return pointer.source_id === "sse" && ["etf_scale", "etf_pcf"].includes(pointer.dataset);
  if (domain === "szse_etf") return pointer.source_id === "szse" && pointer.dataset === "etf_scale";
  return false;
}

function modelApplies(domain: InventoryDomain, row: ModelRow): boolean {
  const definition = INVENTORY_DOMAINS.find((item) => item.id === domain);
  return definition?.market === null || row.market === definition?.market;
}

function modelSourceApplies(domain: InventoryDomain, source: ModelRow["source"]): boolean {
  if (domain === "sse_etf" || domain === "szse_etf") return source === "etf";
  if (domain.endsWith("_margin")) return source === "margin";
  return source === "connect";
}

function releaseSlot(manifest: ReadModelManifest): ReadModelSlot {
  return manifest.slot && READ_MODEL_SLOTS.includes(manifest.slot) ? manifest.slot : "dashboard";
}

type ReleaseEvidence = {
  releaseId: string;
  asOf: string;
  slot: ReadModelSlot;
  status: string;
  schemaVersion: number;
  manifestValid: boolean;
  manifestComplete: boolean;
  manifestHashStatus: "READY" | "HASH_MISSING";
  manifestSha256: string | null;
  requiredArtifacts: string[];
  artifacts: Record<string, Record<string, unknown>>;
};

function releaseEvidence(row: ReleaseRow): { evidence: ReleaseEvidence; manifest: ReadModelManifest | null } {
  const manifestHashStatus = shaStatus(row.manifest_sha256);
  let manifest: ReadModelManifest | null = null;
  try {
    const parsed: unknown = row.manifest_json ? JSON.parse(row.manifest_json) : null;
    if (isReadModelManifest(parsed) && parsed.releaseId === row.release_id && parsed.asOf === row.as_of) {
      manifest = parsed;
    }
  } catch {
    manifest = null;
  }
  const complete = Boolean(manifest && hasCompleteReadModelManifest(manifest));
  const slot = manifest ? releaseSlot(manifest) : ((row.manifest_json && row.manifest_json.includes('"slot":"etf_penetration"')) ? "etf_penetration" : "dashboard");
  const required = manifest?.requiredArtifacts ?? (manifest ? Object.keys(manifest.artifacts) : []);
  const artifacts: Record<string, Record<string, unknown>> = {};
  if (manifest) {
    for (const [name, descriptor] of Object.entries(manifest.artifacts)) {
      artifacts[name] = {
        key: descriptor.key,
        declaredSha256: descriptor.sha256,
        declaredBytes: descriptor.bytes,
        hashStatus: shaStatus(descriptor.sha256),
        status: "UNKNOWN",
      };
    }
  }
  return {
    manifest,
    evidence: {
      releaseId: row.release_id,
      asOf: row.as_of,
      slot,
      status: row.status,
      schemaVersion: Number(row.schema_version),
      manifestValid: Boolean(manifest),
      manifestComplete: complete,
      manifestHashStatus,
      manifestSha256: row.manifest_sha256,
      requiredArtifacts: required,
      artifacts,
    },
  };
}

function publishedReleaseReady(release: ReleaseEvidence): boolean {
  return Boolean(
    (release.status === "ACTIVE" || release.status === "SUPERSEDED") &&
      release.manifestValid && release.manifestComplete && release.manifestHashStatus === "READY" &&
      Object.values(release.artifacts).length > 0 &&
      Object.values(release.artifacts).every((artifact) => artifact.status === "READY"),
  );
}

type RequestState = {
  usage: InventoryUsage;
  incompleteReasons: string[];
};

function addMeta(state: RequestState, meta: D1Meta | undefined): void {
  const rowsRead = safeInteger(meta?.rows_read);
  const rowsWritten = safeInteger(meta?.rows_written);
  if (rowsRead === null) {
    state.usage.d1.rowsReadKnown = false;
    state.usage.d1.rowsRead = null;
  } else state.usage.d1.rowsRead = (state.usage.d1.rowsRead ?? 0) + rowsRead;
  if (rowsWritten === null) {
    state.usage.d1.rowsWrittenKnown = false;
    state.usage.d1.rowsWritten = null;
  } else state.usage.d1.rowsWritten = (state.usage.d1.rowsWritten ?? 0) + rowsWritten;
  if (rowsWritten !== null && rowsWritten > 0) state.incompleteReasons.push("D1_READONLY_VIOLATION");
  if ((state.usage.d1.rowsRead ?? 0) > MAX_D1_ROWS_READ) state.incompleteReasons.push("D1_ROWS_READ_LIMIT");
}

async function readAll<T>(state: RequestState, db: ReadOnlyD1, sql: string, binds: unknown[]): Promise<T[]> {
  if (state.usage.d1.queryCount >= MAX_D1_QUERIES) {
    state.incompleteReasons.push("D1_QUERY_LIMIT");
    return [];
  }
  if (!state.usage.d1.rowsReadKnown || !state.usage.d1.rowsWrittenKnown) {
    state.incompleteReasons.push("D1_ROWS_READ_TELEMETRY_UNKNOWN");
    return [];
  }
  if ((state.usage.d1.rowsRead ?? 0) >= MAX_D1_ROWS_READ) {
    state.incompleteReasons.push("D1_ROWS_READ_LIMIT");
    return [];
  }
  state.usage.d1.queryCount += 1;
  const result = await db.prepare(sql).bind(...binds).all<T>();
  addMeta(state, result.meta);
  if ((state.usage.d1.rowsRead ?? 0) > MAX_D1_ROWS_READ) return [];
  return result.results ?? [];
}

function markModel(
  records: DateInventory[],
  rows: ModelRow[],
): void {
  for (const row of rows) {
    if (!row.row_count || !Number.isSafeInteger(row.row_count)) continue;
    const domains = INVENTORY_DOMAINS.filter((item) => modelSourceApplies(item.id, row.source) && modelApplies(item.id, row));
    for (const definition of domains) {
      const date = records.find((item) => item.dataDate === row.trade_date && item.domain === definition.id);
      if (!date) continue;
      date.evidence.model.push({
        domain: definition.id,
        market: row.market,
        rowCount: row.row_count,
        observedAt: row.latest_observed_at,
      });
      date.stageStatus.model = "READY";
    }
  }
}

function nextIsoDate(value: string): string {
  const parsed = new Date(`${value}T00:00:00Z`);
  parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString().slice(0, 10);
}

function pipelineKeyRange(firstDate: string, lastDate: string): [string, string] {
  return [
    `daily-market-data:${firstDate}:`,
    `daily-market-data:${nextIsoDate(lastDate)}:`,
  ];
}

function pipelineSql(): string {
  return `
    SELECT run_key, pipeline, window_date, window_name, contract_version,
           target_date, status, lease_owner, lease_expires_at,
           source_fingerprint, github_run_id, attempt_count, last_error_code,
           updated_at, completed_at, recovery_id
      FROM pipeline_window
     WHERE run_key >= ? AND run_key < ?
       AND pipeline = ?
     ORDER BY window_date ASC, window_name ASC
     LIMIT ?`;
}

function recoverySql(): string {
  return `
    SELECT recovery_id, run_key, plan_key, plan_sha256, scope_sha256, status,
           latest_receipt_key, latest_receipt_sha256, updated_at
      FROM pipeline_window_recovery
     WHERE run_key >= ? AND run_key < ?
     ORDER BY run_key ASC, updated_at DESC
     LIMIT ?`;
}

function domainForSource(sourceId: string): InventoryDomain | null {
  if (sourceId === "stock_connect_summary") return "hkex_connect";
  if (sourceId === "sse_etf" || sourceId === "sse_etf_nav" || sourceId === "sse_etf_pcf") return "sse_etf";
  if (sourceId === "szse_etf") return "szse_etf";
  if (sourceId === "sse_margin") return "sse_margin";
  if (sourceId === "szse_margin") return "szse_margin";
  if (sourceId === "bse_margin") return "bse_margin";
  return null;
}

function applySourceReadiness(records: DateInventory[], rows: SourceReadinessRow[]): void {
  for (const row of rows) {
    const domain = domainForSource(row.source_id);
    if (!domain) continue;
    const date = records.find((item) => item.dataDate === row.target_date && item.domain === domain);
    if (!date) continue;
    const evidence = parseJsonRecord(row.evidence_json);
    const rawKey = typeof evidence.objectKey === "string"
      ? evidence.objectKey
      : typeof evidence.rawObjectKey === "string" ? evidence.rawObjectKey : null;
    const rawSha = typeof evidence.sha256 === "string"
      ? evidence.sha256
      : typeof evidence.rawSha256 === "string" ? evidence.rawSha256 : null;
    const item = {
      sourceId: row.source_id,
      status: row.status,
      required: Number(row.required) === 1,
      observedAt: row.observed_at,
      deadlineAt: row.deadline_at,
      deadlineExceeded: Number(row.deadline_exceeded) === 1,
      reason: row.reason,
      objectKey: rawKey,
      sha256: rawSha,
      hashStatus: shaStatus(rawSha),
      evidence: sanitizeEvidence(evidence) as Record<string, unknown>,
    };
    date.evidence.raw.push(item);
    if (row.status === "ready" && rawKey && rawSha && SHA256.test(rawSha)) {
      date.stageStatus.raw = "READY";
    } else if (row.status === "error" && sourceUnavailableProvenance(row)) {
      date.stageStatus.raw = "SOURCE_UNAVAILABLE";
    } else if (row.status === "error" && date.stageStatus.raw !== "SOURCE_UNAVAILABLE") {
      date.stageStatus.raw = "UNKNOWN";
    }
  }
}

function applyPointers(records: DateInventory[], rows: PointerRow[]): void {
  for (const row of rows) {
    for (const definition of INVENTORY_DOMAINS) {
      if (!pointerApplies(definition.id, row)) continue;
      const date = records.find((item) => item.dataDate === row.trade_date && item.domain === definition.id);
      if (!date) continue;
      const keyValid = safeKey(row.object_key, "market-data/normalized/");
      const hashStatus = shaStatus(row.sha256);
      const evidence = {
        domain: definition.id,
        sourceId: row.source_id,
        dataset: row.dataset,
        fileName: row.file_name,
        revisionId: row.revision_id,
        objectKey: row.object_key,
        keyValid,
        sha256: row.sha256,
        hashStatus,
        rowCount: row.row_count,
        status: row.status,
        observedAt: row.observed_at,
      };
      if (row.dataset === "etf_pcf") {
        date.evidence.pcfExact.push(evidence);
        if (row.status === "READY" && keyValid && hashStatus === "READY") date.stageStatus.pcf_exact = "READY";
        else if (hashStatus === "HASH_MISSING") date.stageStatus.pcf_exact = "HASH_MISSING";
        else if (row.status !== "READY") date.stageStatus.pcf_exact = "MISSING";
      } else {
        date.evidence.normalized.push(evidence);
        if (row.status === "READY" && keyValid && hashStatus === "READY") date.stageStatus.normalized = "READY";
        else if (hashStatus === "HASH_MISSING") date.stageStatus.normalized = "HASH_MISSING";
        else if (row.status !== "READY" && date.stageStatus.normalized !== "HASH_MISSING") date.stageStatus.normalized = "MISSING";
      }
    }
  }
}

function addR2HeadEvidence(
  state: RequestState,
  releaseEvidenceRows: Array<{ date: DateInventory; release: ReleaseEvidence; manifest: ReadModelManifest | null }>,
  bucket: ReadOnlyR2,
): Promise<void> {
  return (async () => {
    const candidates: Array<{ date: DateInventory; release: ReleaseEvidence; name: string; key: string; sha256: string; bytes: number }> = [];
    for (const row of releaseEvidenceRows) {
      if (!row.manifest) continue;
      const prefix = `market-data/releases/${row.release.asOf}/${row.release.releaseId}/`;
      for (const [name, descriptor] of Object.entries(row.manifest.artifacts)) {
        if (safeKey(descriptor.key, prefix) && SHA256.test(descriptor.sha256)) {
          candidates.push({ date: row.date, release: row.release, name, key: descriptor.key, sha256: descriptor.sha256, bytes: descriptor.bytes });
        } else {
          row.release.artifacts[name] = {
            ...row.release.artifacts[name],
            status: !SHA256.test(descriptor.sha256) ? "HASH_MISSING" : "INCOMPLETE",
          };
          state.incompleteReasons.push("RELEASE_ARTIFACT_KEY_OR_HASH_INVALID");
        }
      }
    }
    candidates.sort((left, right) => left.key.localeCompare(right.key));
    if (candidates.length > MAX_R2_HEADS) {
      state.usage.truncated = true;
      state.incompleteReasons.push("R2_HEAD_LIMIT");
    }
    for (let index = 0; index < candidates.length && state.usage.r2.headCount < MAX_R2_HEADS; index += 1) {
      const candidate = candidates[index];
      state.usage.r2.headCount += 1;
      state.usage.r2.classB += 1;
      let object: R2Head | null;
      try {
        object = await bucket.head(candidate.key);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const reason = /403/.test(message) ? "R2_HEAD_FORBIDDEN" : /429/.test(message) ? "R2_HEAD_RATE_LIMITED" : /timeout|timed out/i.test(message) ? "R2_HEAD_TIMEOUT" : "R2_HEAD_FAILED";
        state.incompleteReasons.push(reason);
        candidate.release.artifacts[candidate.name] = {
          ...candidate.release.artifacts[candidate.name],
          status: "INCOMPLETE",
          error: reason,
        };
        continue;
      }
      const metadata = candidate.release.artifacts[candidate.name];
      if (!object) {
        state.incompleteReasons.push("R2_OBJECT_MISSING");
        candidate.release.artifacts[candidate.name] = { ...metadata, status: "MISSING" };
        continue;
      }
      const actualSha = object.customMetadata?.sha256 ?? null;
      const status = !actualSha
        ? "HASH_MISSING"
        : actualSha !== candidate.sha256 || object.size !== candidate.bytes
          ? "HASH_MISMATCH"
          : "READY";
      if (status !== "READY") state.incompleteReasons.push(`R2_${status}`);
      candidate.release.artifacts[candidate.name] = {
        ...metadata,
        status,
        actualSha256: actualSha,
        actualBytes: object.size,
      };
    }
    if (candidates.length > MAX_R2_HEADS) {
      for (let index = MAX_R2_HEADS; index < candidates.length; index += 1) {
        const candidate = candidates[index];
        candidate.release.artifacts[candidate.name] = {
          ...candidate.release.artifacts[candidate.name],
          status: "INCOMPLETE",
        };
      }
    }
  })();
}

export type RecoveryInventoryResponse = {
  schemaVersion: typeof RECOVERY_INVENTORY_SCHEMA_VERSION;
  contract: typeof RECOVERY_INVENTORY_CONTRACT;
  request: InventoryQuery;
  status: "COMPLETE" | "INCOMPLETE";
  hasMore: boolean;
  nextCursor: string | null;
  activeReleaseSlots: ActiveReleaseRow[];
  dates: Array<{
    dataDate: string;
    domains: DateInventory[];
    releases: Record<ReadModelSlot, ReleaseEvidence[]>;
    sourceReadiness: Record<string, unknown>[];
    artifactPointers: Record<string, unknown>[];
    pipelineWindows: PipelineRow[];
    recoveries: RecoveryRow[];
    marketHistory: MarketHistoryRow | null;
    dateVisible: { status: InventoryStageStatus; evidence: Record<string, unknown>[] };
  }>;
  usage: InventoryUsage;
  incompleteReasons: string[];
};

export async function collectRecoveryInventory(
  runtime: RecoveryInventoryRuntime,
  query: InventoryQuery,
): Promise<RecoveryInventoryResponse> {
  const usage: InventoryUsage = {
    d1: { queryCount: 0, rowsRead: 0, rowsWritten: 0, rowsReadKnown: true, rowsWrittenKnown: true },
    r2: { classA: 0, classB: 0, headCount: 0, headLimit: MAX_R2_HEADS },
    truncated: false,
  };
  const state: RequestState = { usage, incompleteReasons: [] };
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return {
      schemaVersion: RECOVERY_INVENTORY_SCHEMA_VERSION,
      contract: RECOVERY_INVENTORY_CONTRACT,
      request: query,
      status: "INCOMPLETE",
      hasMore: false,
      nextCursor: null,
      activeReleaseSlots: [],
      dates: [],
      usage,
      incompleteReasons: ["STORAGE_UNAVAILABLE"],
    };
  }

  let dateRows: DateRow[];
  try {
    dateRows = await readAll<DateRow>(state, runtime.DB, DATE_CANDIDATES_SQL, [
      query.start, query.end, // historical_snapshot
      query.start, query.end, // release_manifest
      query.start, query.end, // dashboard_snapshot
      query.start, query.end, // market_history_daily
      query.start, query.end, // source_readiness
      query.start, query.end, // history_artifact_pointer
      query.cursor ?? "", query.limit + 1,
    ]);
  } catch {
    state.incompleteReasons.push("D1_READ_FAILED");
    return {
      schemaVersion: RECOVERY_INVENTORY_SCHEMA_VERSION,
      contract: RECOVERY_INVENTORY_CONTRACT,
      request: query,
      status: "INCOMPLETE",
      hasMore: false,
      nextCursor: null,
      activeReleaseSlots: [],
      dates: [],
      usage,
      incompleteReasons: [...new Set(state.incompleteReasons)],
    };
  }
  const hasMore = dateRows.length > query.limit;
  const selected = [...new Set(dateRows.map((row) => row.data_date).filter(validDate))].slice(0, query.limit);
  if (!selected.length) state.incompleteReasons.push("NO_OBSERVED_DATES");
  const firstDate = selected[0];
  const lastDate = selected[selected.length - 1];
  const records = selected.map((dataDate) => INVENTORY_DOMAINS.map((item) => ({
    dataDate,
    ...domainRecord(item.id, item.market),
  }))).flat();
  const outputReleases = new Map<string, { dashboard: ReleaseEvidence[]; etf_penetration: ReleaseEvidence[] }>();
  const releaseEvidenceRows: Array<{ date: DateInventory; release: ReleaseEvidence; manifest: ReadModelManifest | null }> = [];
  const outputPipelines = new Map<string, PipelineRow[]>();
  const outputRecoveries = new Map<string, RecoveryRow[]>();
  const outputMarketHistory = new Map<string, MarketHistoryRow>();
  const outputReadiness = new Map<string, Record<string, unknown>[]>();
  const outputPointers = new Map<string, Record<string, unknown>[]>();
  let activeReleaseSlots: ActiveReleaseRow[] = [];

  if (firstDate && lastDate) {
    try {
      const [pipelineStart, pipelineEnd] = pipelineKeyRange(firstDate, lastDate);
      const expectedPipelineRows = selected.length * PIPELINE_WINDOWS.length;
      // Keep D1 reads sequential.  D1 row telemetry arrives only with each
      // result; issuing all reads through Promise.all would allow later
      // queries to run after the global row budget had already been exceeded.
      const historical = await readAll<HistoricalRow>(state, runtime.DB, HISTORICAL_SQL, [firstDate, lastDate, selected.length + 1]);
      const dashboards = await readAll<DashboardRow>(state, runtime.DB, DASHBOARD_SQL, [firstDate, lastDate, selected.length + 1]);
      const releases = await readAll<ReleaseRow>(state, runtime.DB, RELEASE_SQL, [firstDate, lastDate, MAX_RELEASE_ROWS]);
      const readiness = await readAll<SourceReadinessRow>(state, runtime.DB, SOURCE_READINESS_SQL, [firstDate, lastDate, MAX_SOURCE_READINESS_ROWS]);
      const pointers = await readAll<PointerRow>(state, runtime.DB, POINTER_SQL, [firstDate, lastDate, MAX_POINTER_ROWS]);
      const models = await readAll<ModelRow>(state, runtime.DB, MODEL_SQL, [firstDate, lastDate, firstDate, lastDate, firstDate, lastDate, 900]);
      const marketHistory = await readAll<MarketHistoryRow>(state, runtime.DB, MARKET_HISTORY_SQL, [firstDate, lastDate, selected.length + 1]);
      const pipelines = expectedPipelineRows
        ? await readAll<PipelineRow>(state, runtime.DB, pipelineSql(), [pipelineStart, pipelineEnd, "daily-market-data", expectedPipelineRows + 1])
        : [];
      const recoveries = expectedPipelineRows
        ? await readAll<RecoveryRow>(state, runtime.DB, recoverySql(), [pipelineStart, pipelineEnd, expectedPipelineRows + 1])
        : [];
      const activeReleases = await readAll<ActiveReleaseRow>(state, runtime.DB, ACTIVE_RELEASE_SQL, [2]);
      applySourceReadiness(records, readiness);
      applyPointers(records, pointers);
      markModel(records, models);
      for (const row of readiness) {
        const list = outputReadiness.get(row.target_date) ?? [];
        list.push({
          targetDate: row.target_date,
          sourceId: row.source_id,
          status: row.status,
          required: Number(row.required) === 1,
          observedAt: row.observed_at,
          deadlineAt: row.deadline_at,
          deadlineExceeded: Number(row.deadline_exceeded) === 1,
          observedDates: sanitizeEvidence(parseJsonValue(row.observed_dates_json)),
          watermark: sanitizeEvidence(parseJsonRecord(row.watermark_json)),
          reason: row.reason,
          evidence: sanitizeEvidence(parseJsonRecord(row.evidence_json)),
          updatedAt: row.updated_at,
        });
        outputReadiness.set(row.target_date, list);
      }
      for (const row of pointers) {
        const list = outputPointers.get(row.trade_date) ?? [];
        list.push({
          sourceId: row.source_id,
          dataset: row.dataset,
          tradeDate: row.trade_date,
          fileName: row.file_name,
          revisionId: row.revision_id,
          objectKey: row.object_key,
          keyValid: safeKey(row.object_key, "market-data/"),
          sha256: row.sha256,
          hashStatus: shaStatus(row.sha256),
          contentEncoding: row.content_encoding,
          rowCount: row.row_count,
          observedAt: row.observed_at,
          status: row.status,
        });
        outputPointers.set(row.trade_date, list);
      }
      for (const row of marketHistory) outputMarketHistory.set(row.trade_date, row);
      for (const row of pipelines) {
        const list = outputPipelines.get(row.window_date) ?? [];
        list.push(row);
        outputPipelines.set(row.window_date, list);
      }
      for (const row of recoveries) {
        const windowDate = row.run_key.split(":")[1] ?? "";
        const list = outputRecoveries.get(windowDate) ?? [];
        list.push(row);
        outputRecoveries.set(windowDate, list);
      }
      activeReleaseSlots = activeReleases;

      for (const row of historical) {
        const dateRecords = records.filter((item) => item.dataDate === row.as_of);
        for (const record of dateRecords) {
          record.evidence.dateVisible.push({
            source: "historical_snapshot",
            observedAt: row.observed_at,
            objectKey: row.r2_object_key,
            sha256: row.snapshot_sha256,
            hashStatus: shaStatus(row.snapshot_sha256),
            qualityPass: Number(row.quality_pass) === 1,
            qualityWarn: Number(row.quality_warn),
            qualityFail: Number(row.quality_fail),
          });
        }
      }
      for (const row of dashboards) {
        const dateRecords = records.filter((item) => item.dataDate === row.as_of);
        for (const record of dateRecords) record.evidence.dashboard.push({
          source: "dashboard_snapshot",
          observedAt: row.observed_at,
          publishedAt: row.published_at,
          objectKey: row.r2_object_key,
          qualityFail: Number(row.quality_fail),
        });
      }
      for (const row of releases) {
        const dateRecords = records.filter((item) => item.dataDate === row.as_of);
        if (!dateRecords.length) continue;
        const parsed = releaseEvidence(row);
        if (!parsed.evidence.manifestValid) state.incompleteReasons.push("RELEASE_MANIFEST_INVALID");
        else if (!parsed.evidence.manifestComplete) state.incompleteReasons.push("RELEASE_MANIFEST_INCOMPLETE");
        if (parsed.evidence.manifestHashStatus !== "READY") state.incompleteReasons.push("RELEASE_MANIFEST_HASH_MISSING");
        const date = dateRecords[0];
        const slot = parsed.evidence.slot;
        const slotRecord = outputReleases.get(row.as_of) ?? { dashboard: [], etf_penetration: [] };
        slotRecord[slot].push(parsed.evidence);
        outputReleases.set(row.as_of, slotRecord);
        releaseEvidenceRows.push({ date, release: parsed.evidence, manifest: parsed.manifest });
      }
      await addR2HeadEvidence(state, releaseEvidenceRows, runtime.RAW_BUCKET);
    } catch {
      state.incompleteReasons.push("D1_OR_R2_READ_FAILED");
    }
  }

  const dates = selected.map((dataDate) => {
    const domains = records.filter((item) => item.dataDate === dataDate);
    const slotRecord = outputReleases.get(dataDate) ?? { dashboard: [], etf_penetration: [] };
    const dashboardReady = slotRecord.dashboard.some(publishedReleaseReady);
    const legacyVisible = domains.some((domain) => domain.evidence.dateVisible.some((item) => item.source === "historical_snapshot" && item.qualityPass === true && item.hashStatus === "READY"));
    const dateVisibleStatus: InventoryStageStatus = dashboardReady || legacyVisible ? "READY" : slotRecord.dashboard.length || legacyVisible ? "INCOMPLETE" : "UNKNOWN";
    for (const domain of domains) {
      domain.stageStatus.dashboard = dashboardReady ? "READY" : slotRecord.dashboard.length ? "INCOMPLETE" : "UNKNOWN";
      domain.stageStatus.etf_penetration = slotRecord.etf_penetration.some(publishedReleaseReady) ? "READY" : slotRecord.etf_penetration.length ? "INCOMPLETE" : "UNKNOWN";
      domain.stageStatus.date_visible = dateVisibleStatus;
    }
    return {
      dataDate,
      domains,
      releases: slotRecord,
      sourceReadiness: [...(outputReadiness.get(dataDate) ?? [])],
      artifactPointers: [...(outputPointers.get(dataDate) ?? [])],
      pipelineWindows: [...(outputPipelines.get(dataDate) ?? [])],
      recoveries: [...(outputRecoveries.get(dataDate) ?? [])],
      marketHistory: outputMarketHistory.get(dataDate) ?? null,
      dateVisible: {
        status: dateVisibleStatus,
        evidence: domains.flatMap((domain) => domain.evidence.dateVisible),
      },
    };
  });
  const rowsWrittenViolation = (usage.d1.rowsWritten ?? 0) > 0;
  if (!usage.d1.rowsReadKnown) state.incompleteReasons.push("D1_ROWS_READ_TELEMETRY_UNKNOWN");
  if (!usage.d1.rowsWrittenKnown) state.incompleteReasons.push("D1_ROWS_WRITTEN_TELEMETRY_UNKNOWN");
  if (rowsWrittenViolation) state.incompleteReasons.push("D1_READONLY_VIOLATION");
  const status = state.incompleteReasons.length ? "INCOMPLETE" : "COMPLETE";
  const last = selected[selected.length - 1];
  return {
    schemaVersion: RECOVERY_INVENTORY_SCHEMA_VERSION,
    contract: RECOVERY_INVENTORY_CONTRACT,
    request: query,
    status,
    hasMore,
    nextCursor: hasMore && last ? encodeInventoryCursor(last) : null,
    activeReleaseSlots,
    dates,
    usage,
    incompleteReasons: [...new Set(state.incompleteReasons)],
  };
}
