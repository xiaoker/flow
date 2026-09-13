import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import type { DashboardSnapshot } from "@/app/lib/dashboard-data";
import {
  loadEtfFlowState,
  mergeSnapshotIntoEtfFlowState,
  persistEtfFlowState,
  validateEtfTerminalCoverage,
  type OfficialEtfDailyChange,
} from "@/app/lib/etf-flow-state";
import { extractMarketHistoryPoint } from "@/app/lib/market-history";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";
import { recordSourceObservation } from "@/app/lib/source-observation";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 12_000_000;
const MAX_ARTIFACTS = 16;
const MAX_EXPECTED_ARTIFACTS = 32;
const MAX_NORMALIZED_FILES = 2;
const MAX_ETF_DAILY_CHANGE_ROWS = 10_000;
const HISTORY_RETENTION_DAYS = 0;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const PIPELINE_RUN_KEY = /^daily-market-data:\d{4}-\d{2}-\d{2}:(?:evening|morning_exact|morning|reconcile):v\d+$/;
const REQUIRED_READINESS_DATASETS = new Set<HistoryArtifact["dataset"]>([
  "etf_scale",
  "margin_market_daily",
  "stock_connect_daily",
]);

type NormalizedFile = {
  name: string;
  format: "csv";
  sha256: string;
  rowCount: number;
  tradeDates: string[];
  contentBase64: string;
  contentEncoding?: "gzip";
};

type HistoryArtifact = {
  releaseId: string;
  sourceId: "sse" | "szse" | "bse" | "hkex";
  dataset:
    | "etf_scale"
    | "etf_nav"
    | "etf_pcf"
    | "margin_market_daily"
    | "margin_security_daily"
    | "stock_connect_daily";
  requestedDate: string;
  tradeDates: string[];
  observedAt: string;
  sourceUrl: string;
  dateBasis: string;
  sha256: string;
  contentType: string;
  extension: string;
  rawContentBase64: string;
  metadata: Record<string, unknown>;
  normalized: NormalizedFile[];
};

type OfficialEtfDailyChangeRow = {
  market: "SSE" | "SZSE";
  code: string;
  previousTradeDate: string | null;
  currentShares: number | null;
  previousShares: number | null;
  changeShares: number | null;
  aligned: boolean;
  alignmentReason?:
    | "aligned"
    | "new_listing"
    | "missing_previous"
    | "delisted"
    | "delayed_market";
};

type OfficialEtfDailyChangesContract = {
  schemaVersion: 1;
  asOf: string;
  expectedPreviousDate: string | null;
  sourceReleaseIds: string[];
  inputSha256: string;
  rows: OfficialEtfDailyChangeRow[];
  marketStatus?: Array<{
    market: "SSE" | "SZSE";
    status: "ready" | "delayed_market" | "missing_current" | "missing_previous";
    latestAvailableDate: string | null;
    currentDate: string | null;
    expectedPreviousDate: string | null;
  }>;
  counts?: {
    totalRows: number;
    alignedRows: number;
    newListingRows: number;
    missingPreviousRows: number;
    delistedRows: number;
    delayedMarketRows: number;
  };
};

type HistoryBundle = {
  version: 1;
  history: {
    runId: string;
    startedAt: string;
    finishedAt: string;
    retentionDays: number;
    asOf: string;
    // Resumable non-final chunks intentionally omit the multi-megabyte
    // snapshot.  The final chunk carries it and is the only point at which
    // publication and snapshot validation are performed.
    snapshot?: DashboardSnapshot;
    artifacts: HistoryArtifact[];
    /**
     * Offline, canonical ETF adjacent changes.  PR3 archives this evidence;
     * PR4 may apply it to the R2 flow state when archive-only mode is enabled.
     */
    officialEtfDailyChanges?: OfficialEtfDailyChangesContract;
    /**
     * Chunked collection contract.  Each request may carry one artifact, and
     * the final request sets finalize=true.  The expected ids are persisted in
     * R2 state so a missing exchange/date cannot be mistaken for a complete
     * publication.
     */
    expectedArtifacts?: string[];
    finalize?: boolean;
    /** Stable cross-scheduler identity, carried into shadow receipts only. */
    runKey?: string;
  };
};

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isEnabledFlag(value: string | undefined): boolean {
  const normalized = value?.trim().toLowerCase();
  return normalized === "1" || normalized === "true";
}

function officialDailyChangesFromContract(
  contract: OfficialEtfDailyChangesContract,
): OfficialEtfDailyChange[] {
  return contract.rows.map((row) => ({
    code: row.code,
    market: row.market,
    previousTradeDate: row.previousTradeDate,
    expectedPreviousTradeDate: contract.expectedPreviousDate,
    value: row.aligned && row.changeShares !== null
      ? Number((row.changeShares / 100_000_000).toFixed(6))
      : null,
  }));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isOfficialSource(
  sourceId: HistoryArtifact["sourceId"],
  dataset: HistoryArtifact["dataset"],
  value: string,
): boolean {
  try {
    const hostname = new URL(value).hostname.toLowerCase();
    if (sourceId === "hkex") {
      return dataset === "stock_connect_daily" && hostname === "www.hkex.com.hk";
    }
    if (sourceId === "sse") return hostname === "query.sse.com.cn";
    if (sourceId === "bse") {
      return hostname === "www.bse.cn" || hostname === "www.bseinfo.net";
    }
    return hostname === "www.szse.cn" || hostname === "fund.szse.cn";
  } catch {
    return false;
  }
}

function isNormalizedFile(value: unknown): value is NormalizedFile {
  if (!isRecord(value)) return false;
  return Boolean(
    typeof value.name === "string" &&
      /^[a-z0-9-]{1,24}$/.test(value.name) &&
      value.format === "csv" &&
      typeof value.sha256 === "string" &&
      SHA256.test(value.sha256) &&
      Number.isSafeInteger(value.rowCount) &&
      Number(value.rowCount) >= 0 &&
      Array.isArray(value.tradeDates) &&
      value.tradeDates.length <= 750 &&
      value.tradeDates.every((date) => typeof date === "string" && ISO_DATE.test(date)) &&
      typeof value.contentBase64 === "string" &&
      (value.contentEncoding === undefined || value.contentEncoding === "gzip"),
  );
}

function isHistoryArtifact(value: unknown): value is HistoryArtifact {
  if (!isRecord(value)) return false;
  const sourceId = value.sourceId;
  const dataset = value.dataset;
  if (
    (sourceId !== "sse" && sourceId !== "szse" && sourceId !== "bse" && sourceId !== "hkex") ||
    (dataset !== "etf_scale" &&
      dataset !== "etf_nav" &&
      dataset !== "etf_pcf" &&
      dataset !== "margin_market_daily" &&
      dataset !== "margin_security_daily" &&
      dataset !== "stock_connect_daily")
  ) return false;
  const expectedPair =
    (dataset === "stock_connect_daily" && sourceId === "hkex") ||
    (dataset === "etf_scale" && (sourceId === "sse" || sourceId === "szse")) ||
    (dataset === "etf_nav" && sourceId === "sse") ||
    (dataset === "etf_pcf" && sourceId === "sse") ||
    (dataset === "margin_market_daily" &&
      (sourceId === "sse" || sourceId === "szse" || sourceId === "bse")) ||
    (dataset === "margin_security_daily" && sourceId === "sse");
  if (!expectedPair) return false;
  return Boolean(
    typeof value.requestedDate === "string" &&
      /^[0-9_-]{8,25}$/.test(value.requestedDate) &&
      typeof value.sha256 === "string" &&
      SHA256.test(value.sha256) &&
      value.releaseId === `${dataset}:${sourceId}:${value.requestedDate}:${value.sha256.slice(0, 16)}` &&
      Array.isArray(value.tradeDates) &&
      value.tradeDates.length <= 750 &&
      value.tradeDates.every((date) => typeof date === "string" && ISO_DATE.test(date)) &&
      typeof value.observedAt === "string" &&
      value.observedAt.length > 0 &&
      typeof value.sourceUrl === "string" &&
      isOfficialSource(sourceId, dataset, value.sourceUrl) &&
      typeof value.dateBasis === "string" &&
      value.dateBasis.length > 0 &&
      typeof value.contentType === "string" &&
      value.contentType.length > 0 &&
      typeof value.extension === "string" &&
      /^[a-z0-9]{1,8}$/.test(value.extension) &&
      typeof value.rawContentBase64 === "string" &&
      value.rawContentBase64.length > 0 &&
      isRecord(value.metadata) &&
      value.metadata.sha256 === value.sha256 &&
      Array.isArray(value.normalized) &&
      value.normalized.length > 0 &&
      value.normalized.length <= MAX_NORMALIZED_FILES &&
      value.normalized.every(isNormalizedFile)
  );
}

function isNullableIsoDate(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && ISO_DATE.test(value));
}

function isNullableSafeInteger(value: unknown, { nonNegative = false } = {}): value is number | null {
  return (
    value === null ||
    (typeof value === "number" &&
      Number.isSafeInteger(value) &&
      (!nonNegative || value >= 0))
  );
}

function rowReason(row: OfficialEtfDailyChangeRow): NonNullable<OfficialEtfDailyChangeRow["alignmentReason"]> {
  if (row.alignmentReason) return row.alignmentReason;
  if (row.aligned) return "aligned";
  if (row.currentShares === null) return "delisted";
  if (row.previousTradeDate === null) return "new_listing";
  return "missing_previous";
}

function officialEtfDailyChangesFingerprintInput(
  contract: OfficialEtfDailyChangesContract,
): string {
  const releaseIds = [...new Set(contract.sourceReleaseIds)].sort();
  // Match Python's byte/Unicode ordering without locale-dependent
  // collation.  The fingerprint is a cross-runtime contract, so the
  // comparator must not vary with the Worker locale.
  const rows = [...contract.rows].sort((left, right) => {
    const leftKey = `${left.market}:${left.code}`;
    const rightKey = `${right.market}:${right.code}`;
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
  });
  const nullable = (value: string | number | null): string =>
    value === null || value === undefined ? "" : String(value);
  const lines = [
    contract.asOf,
    contract.expectedPreviousDate ?? "",
    releaseIds.join(","),
    ...rows.map((row) =>
      [
        row.market,
        row.code,
        nullable(row.previousTradeDate),
        nullable(row.currentShares),
        nullable(row.previousShares),
        nullable(row.changeShares),
        row.aligned ? "1" : "0",
        rowReason(row),
      ].join(","),
    ),
  ];
  return lines.join("\n");
}

function isOfficialEtfDailyChanges(
  value: unknown,
  history: HistoryBundle["history"],
): value is OfficialEtfDailyChangesContract {
  if (!isRecord(value) || value.schemaVersion !== 1) return false;
  if (
    typeof value.asOf !== "string" ||
    !ISO_DATE.test(value.asOf) ||
    value.asOf !== history.asOf ||
    !isNullableIsoDate(value.expectedPreviousDate) ||
    (value.expectedPreviousDate !== null && value.expectedPreviousDate >= value.asOf) ||
    !Array.isArray(value.sourceReleaseIds) ||
    value.sourceReleaseIds.length === 0 ||
    value.sourceReleaseIds.length > MAX_EXPECTED_ARTIFACTS ||
    new Set(value.sourceReleaseIds).size !== value.sourceReleaseIds.length ||
    !value.sourceReleaseIds.every(
      (item) =>
        typeof item === "string" &&
        /^etf_scale:(sse|szse):[0-9_-]{8,25}:[a-f0-9]{16}$/.test(item),
    ) ||
    typeof value.inputSha256 !== "string" ||
    !SHA256.test(value.inputSha256) ||
    !Array.isArray(value.rows) ||
    value.rows.length > MAX_ETF_DAILY_CHANGE_ROWS
  ) {
    return false;
  }

  const availableReleaseIds = new Set([
    ...history.artifacts.map((artifact) => artifact.releaseId),
    ...(history.expectedArtifacts ?? []),
  ]);
  if (value.sourceReleaseIds.some((releaseId) => !availableReleaseIds.has(releaseId))) {
    return false;
  }

  const keys = new Set<string>();
  const reasonCounts = {
    aligned: 0,
    new_listing: 0,
    missing_previous: 0,
    delisted: 0,
    delayed_market: 0,
  };
  for (const raw of value.rows) {
    if (!isRecord(raw)) return false;
    const row = raw as Partial<OfficialEtfDailyChangeRow>;
    if (
      (row.market !== "SSE" && row.market !== "SZSE") ||
      typeof row.code !== "string" ||
      !/^\d{6}$/.test(row.code) ||
      !isNullableIsoDate(row.previousTradeDate) ||
      !isNullableSafeInteger(row.currentShares, { nonNegative: true }) ||
      !isNullableSafeInteger(row.previousShares, { nonNegative: true }) ||
      !isNullableSafeInteger(row.changeShares) ||
      typeof row.aligned !== "boolean"
    ) {
      return false;
    }
    const key = `${row.market}:${row.code}`;
    if (keys.has(key)) return false;
    keys.add(key);
    if (row.previousTradeDate !== null && row.previousTradeDate >= value.asOf) {
      return false;
    }
    const reason = rowReason(row as OfficialEtfDailyChangeRow);
    if (!Object.prototype.hasOwnProperty.call(reasonCounts, reason)) return false;
    reasonCounts[reason] += 1;
    if (row.aligned) {
      if (
        value.expectedPreviousDate === null ||
        row.previousTradeDate !== value.expectedPreviousDate ||
        row.currentShares === null ||
        row.previousShares === null ||
        row.changeShares === null ||
        row.currentShares - row.previousShares !== row.changeShares ||
        reason !== "aligned"
      ) {
        return false;
      }
    } else {
      // An unaligned row is evidence, not a guessed daily flow.  Its derived
      // change must remain null regardless of whether a stale prior row is
      // available.
      if (row.changeShares !== null) return false;
      if (reason === "aligned") return false;
      if (reason === "new_listing") {
        if (row.currentShares === null || row.previousTradeDate !== null || row.previousShares !== null) return false;
      } else if (reason === "delisted") {
        if (row.currentShares !== null || row.previousTradeDate === null || row.previousShares === null) return false;
      } else if (reason === "delayed_market") {
        if (row.currentShares === null && (row.previousTradeDate === null || row.previousShares === null)) return false;
      } else if (row.currentShares === null) {
        return false;
      }
    }
  }

  if (value.counts !== undefined) {
    const counts = value.counts;
    if (
      !isRecord(counts) ||
      counts.totalRows !== value.rows.length ||
      counts.alignedRows !== reasonCounts.aligned ||
      counts.newListingRows !== reasonCounts.new_listing ||
      counts.missingPreviousRows !== reasonCounts.missing_previous ||
      counts.delistedRows !== reasonCounts.delisted ||
      counts.delayedMarketRows !== reasonCounts.delayed_market
    ) {
      return false;
    }
  }

  if (value.marketStatus !== undefined) {
    if (!Array.isArray(value.marketStatus) || value.marketStatus.length > 2) return false;
    const markets = new Set<string>();
    for (const rawStatus of value.marketStatus) {
      if (!isRecord(rawStatus)) return false;
      if (
        (rawStatus.market !== "SSE" && rawStatus.market !== "SZSE") ||
        markets.has(rawStatus.market) ||
        !["ready", "delayed_market", "missing_current", "missing_previous"].includes(String(rawStatus.status)) ||
        !isNullableIsoDate(rawStatus.latestAvailableDate) ||
        !isNullableIsoDate(rawStatus.currentDate) ||
        !isNullableIsoDate(rawStatus.expectedPreviousDate) ||
        rawStatus.expectedPreviousDate !== value.expectedPreviousDate
      ) {
        return false;
      }
      if (rawStatus.status === "ready" || rawStatus.status === "missing_previous") {
        if (rawStatus.currentDate !== value.asOf || rawStatus.latestAvailableDate !== value.asOf) return false;
      }
      if (rawStatus.status === "delayed_market" && rawStatus.currentDate === value.asOf) {
        return false;
      }
      markets.add(rawStatus.market);
    }
  }
  return true;
}

async function validateOfficialEtfDailyChangesHash(
  contract: OfficialEtfDailyChangesContract,
): Promise<boolean> {
  const digest = await sha256Hex(
    new TextEncoder().encode(officialEtfDailyChangesFingerprintInput(contract)),
  );
  return digest === contract.inputSha256;
}

function isHistoryBundle(value: unknown): value is HistoryBundle {
  if (!isRecord(value) || value.version !== 1 || !isRecord(value.history)) return false;
  const history = value.history;
  const expectedArtifacts = history.expectedArtifacts;
  const finalizeRequested = history.finalize !== false;
  const expectedArtifactsValid =
    expectedArtifacts === undefined ||
    (Array.isArray(expectedArtifacts) &&
      expectedArtifacts.length > 0 &&
      expectedArtifacts.length <= MAX_EXPECTED_ARTIFACTS &&
      new Set(expectedArtifacts).size === expectedArtifacts.length &&
      expectedArtifacts.every(
        (item) =>
          typeof item === "string" &&
          /^[A-Za-z0-9+:_.-]{6,180}$/.test(item),
      ));
  return Boolean(
    typeof history.runId === "string" &&
      /^[A-Za-z0-9+:_-]{6,100}$/.test(history.runId) &&
      typeof history.startedAt === "string" &&
      history.startedAt.length > 0 &&
      typeof history.finishedAt === "string" &&
      history.finishedAt.length > 0 &&
      history.retentionDays === HISTORY_RETENTION_DAYS &&
      typeof history.asOf === "string" &&
      ISO_DATE.test(history.asOf) &&
      Array.isArray(history.artifacts) &&
      history.artifacts.length > 0 &&
      history.artifacts.length <= MAX_ARTIFACTS &&
      history.artifacts.every(isHistoryArtifact) &&
      (history.officialEtfDailyChanges === undefined ||
        isOfficialEtfDailyChanges(history.officialEtfDailyChanges, history)) &&
      expectedArtifactsValid &&
      (history.finalize === undefined || typeof history.finalize === "boolean") &&
      (history.runKey === undefined ||
        (typeof history.runKey === "string" && PIPELINE_RUN_KEY.test(history.runKey))) &&
      (isDashboardSnapshot(history.snapshot) ||
        (!finalizeRequested && history.snapshot === undefined)) &&
      (history.snapshot === undefined || history.snapshot.asOf === history.asOf)
  );
}

const D1_BATCH_SIZE = 200;

/** Execute D1 writes in bounded batches, with a small test/compatibility
 * fallback for local adapters that only implement prepare().run(). */
async function executeD1Statements(
  db: D1Database,
  statements: D1PreparedStatement[],
): Promise<void> {
  for (let offset = 0; offset < statements.length; offset += D1_BATCH_SIZE) {
    const batch = statements.slice(offset, offset + D1_BATCH_SIZE);
    if (typeof db.batch === "function") {
      await db.batch(batch);
      continue;
    }
    for (const statement of batch) await statement.run();
  }
}

/**
 * The phase-0 readiness ledger is written before the archive request, so its
 * first version only knows local runner paths.  Once an artifact is accepted
 * by this route, the immutable R2 key and digest become the authoritative
 * provenance receipt.  Keep that receipt in the existing control-plane
 * tables instead of asking the read-only inventory to infer it from a model
 * row or a UI release.
 */
function canonicalTradeDate(value: string): string | null {
  if (ISO_DATE.test(value)) return value;
  if (/^\d{8}$/.test(value)) {
    const candidate = `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
    return ISO_DATE.test(candidate) ? candidate : null;
  }
  return null;
}

function canonicalObservedAt(value: string): string {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? new Date().toISOString() : new Date(parsed).toISOString();
}

function readinessSourceId(artifact: HistoryArtifact): string | null {
  if (artifact.dataset === "etf_scale") return `${artifact.sourceId}_etf`;
  if (artifact.dataset === "etf_nav") return "sse_etf_nav";
  if (artifact.dataset === "etf_pcf") return "sse_etf_pcf";
  if (artifact.dataset === "margin_market_daily") return `${artifact.sourceId}_margin`;
  if (artifact.dataset === "stock_connect_daily") return "stock_connect_summary";
  // Security-level margin detail is an optional enrichment and must not
  // overwrite the market-level readiness watermark with a different feed.
  return null;
}

function pointerDataset(artifact: HistoryArtifact): "etf_scale" | "etf_pcf" | null {
  // Keep this first repair bounded to the pointer datasets already consumed
  // by the inventory reader.  Margin/connect pointers need a separate
  // contract review before they can affect recovery stage semantics.
  return artifact.dataset === "etf_scale" || artifact.dataset === "etf_pcf"
    ? artifact.dataset
    : null;
}

async function persistArchivedArtifactEvidence(
  db: D1Database,
  artifact: HistoryArtifact,
  normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array }>,
  rawKey: string,
  normalizedKeys: string[],
  runId: string,
): Promise<{ pointerCount: number; readinessCount: number }> {
  const observedAt = canonicalObservedAt(artifact.observedAt);
  const updatedAt = new Date().toISOString();
  const dates = [...new Set(
    normalizedFiles.flatMap(({ file }) => file.tradeDates.map(canonicalTradeDate).filter((date): date is string => Boolean(date))),
  )].sort();
  const pointerStatements: D1PreparedStatement[] = [];
  const dataset = pointerDataset(artifact);
  if (dataset) {
    normalizedFiles.forEach(({ file }, index) => {
      const objectKey = normalizedKeys[index];
      if (!objectKey) return;
      for (const tradeDate of file.tradeDates
        .map(canonicalTradeDate)
        .filter((date): date is string => Boolean(date))) {
        pointerStatements.push(
          db.prepare(
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
          ).bind(
            artifact.sourceId,
            dataset,
            tradeDate,
            file.name,
            artifact.releaseId,
            objectKey,
            file.sha256,
            file.contentEncoding === "gzip" ? "gzip" : null,
            file.rowCount,
            observedAt,
            updatedAt,
          ),
        );
      }
    });
  }

  const sourceId = readinessSourceId(artifact);
  const readinessStatements: D1PreparedStatement[] = [];
  if (sourceId && dates.length) {
    for (const tradeDate of dates) {
      const evidence = {
        archiveVerified: true,
        objectKey: rawKey,
        rawObjectKey: rawKey,
        sha256: artifact.sha256,
        rawSha256: artifact.sha256,
        normalizedKeys,
        normalizedSha256: normalizedFiles.map(({ file }) => ({
          name: file.name,
          sha256: file.sha256,
          rowCount: file.rowCount,
        })),
        sourceBusinessDate: tradeDate,
        sourceFingerprint: artifact.sha256,
        evidenceSource: "history-import",
        evidenceCapturedAt: observedAt,
      };
      readinessStatements.push(
        db.prepare(
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
        ).bind(
          tradeDate,
          sourceId,
          REQUIRED_READINESS_DATASETS.has(artifact.dataset) ? 1 : 0,
          observedAt,
          observedAt,
          JSON.stringify(dates),
          JSON.stringify({ observedTradeDate: tradeDate }),
          "official_source_archived",
          runId,
          JSON.stringify(evidence),
          updatedAt,
        ),
      );
    }
  }

  await executeD1Statements(db, [...pointerStatements, ...readinessStatements]);
  return { pointerCount: pointerStatements.length, readinessCount: readinessStatements.length };
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("invalid_base64");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function decodeNormalizedContent(file: NormalizedFile): Promise<Uint8Array> {
  const encoded = decodeBase64(file.contentBase64);
  if (file.contentEncoding !== "gzip") return encoded;
  const stream = new Blob([encoded]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function parseCsv(content: Uint8Array): Array<Record<string, string>> {
  const text = new TextDecoder().decode(content).replace(/^\uFEFF/, "");
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        field += character;
      }
    } else if (character === '"') {
      quoted = true;
    } else if (character === ",") {
      row.push(field);
      field = "";
    } else if (character === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }
  if (quoted) throw new Error("unterminated_csv_quote");
  if (field.length || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  const headers = rows.shift()?.map((item) => item.trim()) ?? [];
  if (!headers.length) return [];
  return rows
    .filter((values) => values.some((item) => item.length > 0))
    .map((values) => Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""])));
}

function requiredInteger(row: Record<string, string>, field: string): number {
  if (!row[field]) throw new Error(`missing_integer:${field}`);
  const value = Number(row[field]);
  const rounded = Math.round(value);
  // Official values are integer yuan, but pandas may serialize a multiplied
  // decimal such as 78.99 * 1e8 as 7898999999.999999. Only absorb sub-cent
  // IEEE-754 noise; never coerce a materially fractional or missing value.
  if (
    !Number.isFinite(value) ||
    !Number.isSafeInteger(rounded) ||
    Math.abs(value - rounded) > 0.01
  ) {
    throw new Error(`invalid_integer:${field}`);
  }
  return rounded;
}

function optionalInteger(row: Record<string, string>, field: string): number | null {
  if (!row[field]) return null;
  return requiredInteger(row, field);
}

function validateStandardRow(
  artifact: HistoryArtifact,
  row: Record<string, string>,
): void {
  if (!ISO_DATE.test(row.trade_date ?? "")) throw new Error("invalid_standard_trade_date");
  if (row.source_id !== artifact.sourceId) throw new Error("standard_source_mismatch");
  if (!row.observed_at) throw new Error("missing_standard_observed_at");
}

async function loadOfficialEtfDailyChanges(
  db: D1Database,
  asOf: string,
): Promise<OfficialEtfDailyChange[]> {
  const result = await db
    .prepare(
      `WITH current_rows AS (
         SELECT instrument_code, market, shares,
                ROW_NUMBER() OVER (
                  PARTITION BY instrument_code, market
                  ORDER BY observed_at DESC, release_id DESC
                ) AS row_number
         FROM etf_daily
         WHERE trade_date = ?
       ), previous_rows AS (
         SELECT instrument_code, market, shares,
                trade_date AS previous_trade_date,
                ROW_NUMBER() OVER (
                  PARTITION BY instrument_code, market
                  ORDER BY trade_date DESC, observed_at DESC, release_id DESC
                ) AS row_number
         FROM etf_daily
         WHERE trade_date < ?
       ), expected_previous AS (
         SELECT MAX(trade_date) AS expected_previous_trade_date
         FROM (
           SELECT trade_date FROM etf_daily
           WHERE market = 'SSE' AND trade_date < ?
           UNION
           SELECT trade_date FROM margin_daily
           WHERE market = 'SSE' AND trade_date < ?
           UNION
           SELECT trade_date FROM connect_daily
           WHERE market = 'SSE' AND trade_date < ?
         ) AS official_calendar
       )
       SELECT current_rows.instrument_code, current_rows.market,
              current_rows.shares AS current_shares,
              previous_rows.shares AS previous_shares,
              previous_rows.previous_trade_date,
              expected_previous.expected_previous_trade_date
       FROM current_rows
       LEFT JOIN previous_rows
         ON previous_rows.instrument_code = current_rows.instrument_code
        AND previous_rows.market = current_rows.market
        AND previous_rows.row_number = 1
       CROSS JOIN expected_previous
       WHERE current_rows.row_number = 1`,
    )
    .bind(asOf, asOf, asOf, asOf, asOf)
    .all<{
      instrument_code: string;
      market: "SSE" | "SZSE";
      current_shares: number;
      previous_shares: number | null;
      previous_trade_date: string | null;
      expected_previous_trade_date: string | null;
    }>();
  return (result.results ?? []).flatMap((row) => {
    if ((row.market !== "SSE" && row.market !== "SZSE") || !/^\d{6}$/.test(row.instrument_code)) return [];
    const aligned =
      row.previous_trade_date !== null &&
      row.expected_previous_trade_date !== null &&
      row.previous_trade_date === row.expected_previous_trade_date;
    return [{
      code: row.instrument_code,
      market: row.market,
      previousTradeDate: row.previous_trade_date,
      expectedPreviousTradeDate: row.expected_previous_trade_date,
      // A market-specific row may have an older last snapshot than the
      // official calendar anchor (for example SZSE after a delayed release).
      // Keep the evidence, but never label that multi-day delta as 1d.
      value: !aligned || row.previous_shares === null
        ? null
        : Number(((row.current_shares - row.previous_shares) / 100_000_000).toFixed(6)),
    }];
  });
}

async function persistStandardRows(
  db: D1Database,
  artifact: HistoryArtifact,
  normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array }>,
  options: { writeEtfDaily?: boolean } = {},
): Promise<number> {
  if (artifact.dataset === "etf_nav") {
    const rows = normalizedFiles.flatMap(({ file, content }) => {
      const parsed = parseCsv(content);
      if (parsed.length !== file.rowCount) throw new Error("etf_nav_row_count_mismatch");
      return parsed;
    });
    for (const row of rows) {
      validateStandardRow(artifact, row);
      const code = String(row.instrument_code ?? "").trim().padStart(6, "0");
      const nav = Number(row.nav);
      if (!/^\d{6}$/.test(code) || !row.instrument_name || !Number.isFinite(nav) || nav < 0) {
        throw new Error("invalid_etf_nav_row");
      }
      if (!ISO_DATE.test(row.nav_date ?? "")) throw new Error("invalid_etf_nav_date");
    }
    // NAV is a source-enrichment feed.  It is archived in R2 and joined to
    // etf_daily during the collector bundle build; it does not create a
    // second D1 release or overwrite the shares source.
    return rows.length;
  }

  if (artifact.dataset === "etf_pcf") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "data");
    if (!normalized) throw new Error("missing_etf_pcf_data");
    // PCF is validated row-by-row offline (including raw-response replay and
    // duplicate component keys).  Do not materialize hundreds of thousands of
    // component objects in the Worker: verify the decompressed CSV header and
    // its signed content hash here, then archive it without D1 insertion.
    const text = new TextDecoder().decode(normalized.content).replace(/^\uFEFF/, "");
    const header = text.split(/\r?\n/, 1)[0].split(",").map((item) => item.trim());
    const requiredHeaders = ["trade_date", "market", "etf_code", "stock_code", "quantity", "creation_unit", "stock_name"];
    if (requiredHeaders.some((field) => !header.includes(field))) {
      throw new Error("invalid_etf_pcf_header");
    }
    // PCF is an immutable raw/normalized evidence feed.  It deliberately does
    // not create one D1 row per component; the compact stock-flow read model
    // is published with the verified snapshot instead.
    return normalized.file.rowCount;
  }

  if (artifact.dataset === "etf_scale") {
    const market = artifact.sourceId === "sse" ? "SSE" : artifact.sourceId === "szse" ? "SZSE" : null;
    if (!market) throw new Error("invalid_etf_market_source");
    const rows = normalizedFiles.flatMap(({ file, content }) => {
      const parsed = parseCsv(content);
      if (parsed.length !== file.rowCount) throw new Error("etf_row_count_mismatch");
      return parsed;
    });
    if (!rows.length) throw new Error("missing_etf_data");
    const values = rows.map((row) => {
      validateStandardRow(artifact, row);
      const code = String(row.instrument_code ?? "").trim().padStart(6, "0");
      if (!/^\d{6}$/.test(code) || !row.instrument_name) throw new Error("invalid_etf_identity");
      const nav = row.nav === undefined || row.nav === "" ? null : Number(row.nav);
      if (nav !== null && !Number.isFinite(nav)) throw new Error("invalid_etf_nav");
      const navDate = row.nav_date === undefined || row.nav_date === "" ? null : row.nav_date;
      if (navDate !== null && !ISO_DATE.test(navDate)) throw new Error("invalid_etf_nav_date");
      return {
        tradeDate: row.trade_date,
        code,
        name: row.instrument_name,
        category: row.investment_type || row.etf_type || null,
        shares: requiredInteger(row, "shares"),
        navMicros: nav === null ? null : Math.round(nav * 1_000_000),
        navDate,
        dateBasis: row.date_basis,
        observedAt: row.observed_at,
      };
    });
    if (options.writeEtfDaily === false) return rows.length;
    const statements: D1PreparedStatement[] = [
      db.prepare("DELETE FROM etf_daily WHERE release_id = ?").bind(artifact.releaseId),
      ...values.map((value) =>
        db
          .prepare(
            `INSERT INTO etf_daily (
               release_id, trade_date, market, instrument_code, instrument_name,
               category, shares, nav_micros, nav_date, date_basis, source_id, observed_at
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .bind(
            artifact.releaseId,
            value.tradeDate,
            market,
            value.code,
            value.name,
            value.category,
            value.shares,
            value.navMicros,
            value.navDate,
            value.dateBasis,
            artifact.sourceId,
            value.observedAt,
          ),
      ),
    ];
    await executeD1Statements(db, statements);
    return rows.length;
  }

  if (artifact.dataset === "stock_connect_daily") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "summary");
    if (!normalized) throw new Error("missing_connect_summary");
    const rows = parseCsv(normalized.content);
    if (rows.length !== normalized.file.rowCount) throw new Error("connect_row_count_mismatch");
    const values = rows.map((row) => {
      validateStandardRow(artifact, row);
      if (!["SSE", "SZSE"].includes(row.market) || !["northbound", "southbound"].includes(row.direction)) {
        throw new Error("invalid_connect_channel");
      }
      return {
        tradeDate: row.trade_date,
        market: row.market,
        direction: row.direction,
        currency: row.currency,
        totalTurnover: requiredInteger(row, "total_turnover"),
        buyTurnover: optionalInteger(row, "buy_turnover"),
        sellTurnover: optionalInteger(row, "sell_turnover"),
        netBuy: optionalInteger(row, "net_buy"),
        etfTurnover: requiredInteger(row, "etf_turnover"),
        observedAt: row.observed_at,
      };
    });
    const statements: D1PreparedStatement[] = [
      db.prepare("DELETE FROM connect_daily WHERE release_id = ?").bind(artifact.releaseId),
      ...values.map((value) =>
        db
          .prepare(
            `INSERT INTO connect_daily (
               release_id, trade_date, market, direction, currency, total_turnover,
               buy_turnover, sell_turnover, net_buy, etf_turnover, source_id, observed_at
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .bind(
            artifact.releaseId,
            value.tradeDate,
            value.market,
            value.direction,
            value.currency,
            value.totalTurnover,
            value.buyTurnover,
            value.sellTurnover,
            value.netBuy,
            value.etfTurnover,
            artifact.sourceId,
            value.observedAt,
          ),
      ),
    ];
    await executeD1Statements(db, statements);
    return rows.length;
  }

  if (artifact.dataset === "margin_market_daily") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "data");
    if (!normalized) throw new Error("missing_margin_data");
    const rows = parseCsv(normalized.content);
    if (rows.length !== normalized.file.rowCount) throw new Error("margin_row_count_mismatch");
    const values = rows.map((row) => {
      validateStandardRow(artifact, row);
      if (!["SSE", "SZSE", "BSE"].includes(row.market)) throw new Error("invalid_margin_market");
      return {
        tradeDate: row.trade_date,
        market: row.market,
        financingBalance: requiredInteger(row, "financing_balance_yuan"),
        financingBuy: requiredInteger(row, "financing_buy_yuan"),
        lendingBalance: requiredInteger(row, "securities_lending_balance_yuan"),
        totalBalance: requiredInteger(row, "margin_total_balance_yuan"),
        observedAt: row.observed_at,
      };
    });
    const statements: D1PreparedStatement[] = [
      db.prepare("DELETE FROM margin_daily WHERE release_id = ?").bind(artifact.releaseId),
      ...values.map((value) =>
        db
          .prepare(
            `INSERT INTO margin_daily (
               release_id, trade_date, market, financing_balance_yuan,
               financing_buy_yuan, securities_lending_balance_yuan,
               margin_total_balance_yuan, source_id, observed_at
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          )
          .bind(
            artifact.releaseId,
            value.tradeDate,
            value.market,
            value.financingBalance,
            value.financingBuy,
            value.lendingBalance,
            value.totalBalance,
            artifact.sourceId,
            value.observedAt,
          ),
      ),
    ];
    await executeD1Statements(db, statements);
    return rows.length;
  }

  if (artifact.dataset === "margin_security_daily") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "data");
    if (!normalized) throw new Error("missing_margin_security_data");
    const rows = parseCsv(normalized.content);
    if (rows.length !== normalized.file.rowCount) {
      throw new Error("margin_security_row_count_mismatch");
    }
    if (!rows.length) throw new Error("missing_margin_security_data");
    for (const row of rows) {
      validateStandardRow(artifact, row);
      if (
        row.market !== "SSE" ||
        !/^\d{6}$/.test(String(row.stock_code ?? "").trim()) ||
        !row.security_name ||
        !ISO_DATE.test(row.source_trade_date ?? "") ||
        row.source_trade_date !== row.trade_date
      ) {
        throw new Error("invalid_margin_security_identity");
      }
      requiredInteger(row, "financing_balance_yuan");
      requiredInteger(row, "financing_buy_yuan");
      requiredInteger(row, "financing_repayment_yuan");
      requiredInteger(row, "securities_lending_balance_shares");
      requiredInteger(row, "securities_lending_sell_shares");
      requiredInteger(row, "securities_lending_repayment_shares");
    }
    // Per-security detail is an immutable evidence feed used by the offline
    // ETF-to-stock pressure build.  Keep it in R2 without expanding the D1
    // schema; the raw and normalized objects remain independently auditable.
    return rows.length;
  }

  return 0;
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", content);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function putImmutableRaw(
  bucket: R2Bucket,
  key: string,
  content: Uint8Array | string,
  sha256: string,
  options: R2PutOptions,
): Promise<"created" | "existing"> {
  const canHead = typeof bucket.head === "function";
  const existing = canHead ? await bucket.head(key) : null;
  if (existing) {
    if (!key.includes(sha256) && existing.customMetadata?.sha256 !== sha256) {
      throw new Error(`immutable_raw_conflict:${key}`);
    }
    return "existing";
  }
  try {
    await bucket.put(key, content, options);
    return "created";
  } catch (error) {
    const raced = canHead ? await bucket.head(key) : null;
    if (raced && (key.includes(sha256) || raced.customMetadata?.sha256 === sha256)) {
      return "existing";
    }
    throw error;
  }
}

function collectionStateKey(runId: string, asOf: string, releaseId: string): string {
  return `market-data/collection/${runId}/${asOf}/${releaseId}.json`;
}

async function writeCollectionState(
  bucket: R2Bucket,
  input: {
    runId: string;
    asOf: string;
    releaseId: string;
    sourceId: string;
    dataset: string;
    requestedDate: string;
    status: "PENDING" | "VERIFIED" | "FAILED" | "PUBLISHED";
    rawKey?: string;
    normalizedKeys?: string[];
    rowCount?: number;
    error?: string;
  },
): Promise<string> {
  const key = collectionStateKey(input.runId, input.asOf, input.releaseId);
  await bucket.put(
    key,
    JSON.stringify({ ...input, updatedAt: new Date().toISOString() }),
    {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        runId: input.runId,
        asOf: input.asOf,
        releaseId: input.releaseId,
        status: input.status,
      },
    },
  );
  return key;
}

async function checkCollectionComplete(
  bucket: R2Bucket,
  runId: string,
  asOf: string,
  expectedArtifacts: string[] | undefined,
): Promise<{ complete: boolean; pending: string[] }> {
  // Legacy whole-bundle callers do not provide an expected set. Their
  // validated request is already the atomic unit and remains compatible.
  if (
    !expectedArtifacts?.length ||
    (typeof bucket.head !== "function" && typeof bucket.get !== "function")
  ) {
    return { complete: true, pending: [] };
  }
  const checks = await Promise.all(
    expectedArtifacts.map(async (releaseId) => {
      const key = collectionStateKey(runId, asOf, releaseId);
      if (typeof bucket.get === "function") {
        const object = await bucket.get(key);
        if (!object) return { releaseId, exists: false };
        try {
          const state = JSON.parse(await object.text()) as { status?: string };
          return {
            releaseId,
            exists: state.status === "VERIFIED" || state.status === "PUBLISHED",
          };
        } catch {
          return { releaseId, exists: false };
        }
      }
      return {
        releaseId,
        exists: Boolean(await bucket.head!(key)),
      };
    }),
  );
  return {
    complete: checks.every((item) => item.exists),
    pending: checks.filter((item) => !item.exists).map((item) => item.releaseId),
  };
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const archiveOnly = isEnabledFlag(runtime.HISTORY_IMPORT_ARCHIVE_ONLY);
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return NextResponse.json({ error: "storage_unavailable" }, { status: 503 });
  }
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!isHistoryBundle(value)) {
    return NextResponse.json({ error: "invalid_history_bundle" }, { status: 422 });
  }
  if (
    value.history.officialEtfDailyChanges &&
    !(await validateOfficialEtfDailyChangesHash(value.history.officialEtfDailyChanges))
  ) {
    return NextResponse.json(
      { error: "invalid_official_etf_daily_changes_hash" },
      { status: 422 },
    );
  }

  try {
    const { history } = value;
    const officialEtfDailyChanges = history.officialEtfDailyChanges;
    const finalizeRequested = history.finalize !== false;
    const snapshot = history.snapshot;
    const etfQualityIssues = snapshot
      ? validateEtfTerminalCoverage(snapshot.etfTerminal)
      : [];
    const publishHistory = snapshot
      ? isPublishableSnapshot(snapshot) && etfQualityIssues.length === 0
      : false;
    // Pending/fallback snapshots are valid archive evidence but are not
    // publishable releases.  They may legitimately have no adjacent ETF
    // contract after future-dated source responses are excluded by the
    // collector.  Require the compact contract only at the actual publish
    // boundary, so a missing optional derivative cannot abort raw archiving.
    if (archiveOnly && finalizeRequested && snapshot && publishHistory && !officialEtfDailyChanges) {
      return NextResponse.json({ error: "missing_official_etf_daily_changes" }, { status: 422 });
    }
    const expectedArtifacts = history.expectedArtifacts;
    const manifestArtifacts: Array<Record<string, unknown>> = [];
    const collectionStateKeys: string[] = [];
    let archivedPointerCount = 0;
    let archivedReadinessCount = 0;
    let standardizedRows = 0;
    let sourceObservationCount = 0;
    let sourceObservationD1Unavailable = 0;
    let sourceObservationR2Errors = 0;
    for (const artifact of history.artifacts) {
      try {
        const raw = decodeBase64(artifact.rawContentBase64);
        if ((await sha256Hex(raw)) !== artifact.sha256) {
          throw new Error(`raw_sha256_mismatch:${artifact.releaseId}`);
        }
        const prefix = `market-data/raw/${artifact.sourceId}/${artifact.dataset}/${artifact.requestedDate}/${artifact.sha256}`;
        const rawKey = `${prefix}.${artifact.extension}`;
        const metadataKey = `${prefix}.metadata.json`;
        await putImmutableRaw(runtime.RAW_BUCKET, rawKey, raw, artifact.sha256, {
          httpMetadata: { contentType: artifact.contentType },
          customMetadata: {
            sourceId: artifact.sourceId,
            dataset: artifact.dataset,
            requestedDate: artifact.requestedDate,
            sha256: artifact.sha256,
          },
        });
        await putImmutableRaw(
          runtime.RAW_BUCKET,
          metadataKey,
          JSON.stringify({ ...artifact.metadata, release_id: artifact.releaseId, archived_raw_key: rawKey }),
          artifact.sha256,
          {
            httpMetadata: { contentType: "application/json; charset=utf-8" },
            customMetadata: { sha256: artifact.sha256 },
          },
        );
        // v0.5 shadow mode records the immutable observation immediately
        // after the raw object is safely archived.  It deliberately does not
        // participate in publication or readiness decisions yet: D1 is a
        // bounded projection and a later reconcile can replay the same R2
        // receipt if this insert is temporarily unavailable.
        if (isEnabledFlag(runtime.SOURCE_OBSERVATION_SHADOW_ENABLED)) {
          try {
            const observation = await recordSourceObservation(runtime.RAW_BUCKET, runtime.DB, {
              sourceId: artifact.sourceId,
              dataset: artifact.dataset,
              targetDate: history.asOf,
              requestedDate: artifact.requestedDate,
              tradeDates: artifact.tradeDates,
              observedAt: artifact.observedAt,
              sourceUrl: artifact.sourceUrl,
              dateBasis: artifact.dateBasis,
              sha256: artifact.sha256,
              contentType: artifact.contentType,
              rawKey,
              rawBytes: raw.byteLength,
              metadata: artifact.metadata,
              releaseId: artifact.releaseId,
              archiveRunId: history.runId,
              runKey: history.runKey ?? null,
            });
            sourceObservationCount += 1;
            if (observation.d1Status === "unavailable") sourceObservationD1Unavailable += 1;
          } catch (error) {
            // A raw archive without a receipt is not publishable while the
            // shadow contract is enabled.  Continue this bounded artifact
            // loop so other sources can leave independent evidence, then
            // return a retryable response before any release work begins.
            sourceObservationR2Errors += 1;
            const code = error instanceof Error ? error.message : "unknown_receipt_error";
            console.error("source observation receipt failed", {
              sourceId: artifact.sourceId,
              dataset: artifact.dataset,
              requestedDate: artifact.requestedDate,
              code,
            });
          }
        }
        const normalizedKeys: string[] = [];
        const normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array; wireContent: Uint8Array }> = [];
        for (const normalized of artifact.normalized) {
          const wireContent = decodeBase64(normalized.contentBase64);
          const content = await decodeNormalizedContent(normalized);
          if ((await sha256Hex(content)) !== normalized.sha256) {
            throw new Error(`normalized_sha256_mismatch:${artifact.releaseId}:${normalized.name}`);
          }
          const key = `market-data/normalized/${artifact.sourceId}/${artifact.dataset}/${artifact.requestedDate}/${artifact.sha256}/${normalized.name}${normalized.contentEncoding === "gzip" ? ".csv.gz" : ".csv"}`;
          await runtime.RAW_BUCKET.put(key, normalized.contentEncoding === "gzip" ? wireContent : content, {
            httpMetadata: {
              contentType: normalized.contentEncoding === "gzip"
                ? "application/gzip"
                : "text/csv; charset=utf-8",
            },
            customMetadata: {
              sourceId: artifact.sourceId,
              dataset: artifact.dataset,
              rowCount: String(normalized.rowCount),
              sha256: normalized.sha256,
            },
          });
          normalizedKeys.push(key);
          normalizedFiles.push({ file: normalized, content, wireContent });
        }
        const normalizedRowCount = normalizedFiles.reduce(
          (sum, item) => sum + item.file.rowCount,
          0,
        );
        // NAV and PCF are optional enrichment feeds.  An exchange may return
        // an empty exact-date list because its public page only retains the
        // latest bulletin; archive that evidence as VERIFIED so an empty
        // enrichment feed cannot block the complete ETF share publication.
        if (normalizedRowCount === 0 && artifact.dataset !== "etf_nav" && artifact.dataset !== "etf_pcf") {
          const stateKey = await writeCollectionState(runtime.RAW_BUCKET, {
            runId: history.runId,
            asOf: history.asOf,
            releaseId: artifact.releaseId,
            sourceId: artifact.sourceId,
            dataset: artifact.dataset,
            requestedDate: artifact.requestedDate,
            status: "PENDING",
            rawKey,
            normalizedKeys,
            rowCount: 0,
            error: "source_not_available_yet",
          });
          collectionStateKeys.push(stateKey);
          manifestArtifacts.push({
            releaseId: artifact.releaseId,
            sourceId: artifact.sourceId,
            dataset: artifact.dataset,
            requestedDate: artifact.requestedDate,
            tradeDates: artifact.tradeDates,
            observedAt: artifact.observedAt,
            rawKey,
            metadataKey,
            normalizedKeys,
            stateKey,
            status: "PENDING",
          });
          continue;
        }
        try {
          standardizedRows += await persistStandardRows(
            runtime.DB,
            artifact,
            normalizedFiles,
            { writeEtfDaily: !archiveOnly },
          );
        } catch (error) {
          const detail = error instanceof Error ? error.message : "unknown_standardization_error";
          throw new Error(
            `standardize_failed:${artifact.dataset}:${artifact.sourceId}:${artifact.requestedDate}:${detail}`,
          );
        }
        const stateKey = await writeCollectionState(runtime.RAW_BUCKET, {
          runId: history.runId,
          asOf: history.asOf,
          releaseId: artifact.releaseId,
          sourceId: artifact.sourceId,
          dataset: artifact.dataset,
          requestedDate: artifact.requestedDate,
          status: "VERIFIED",
          rawKey,
          normalizedKeys,
          rowCount: normalizedRowCount,
        });
        collectionStateKeys.push(stateKey);
        const evidenceCounts = await persistArchivedArtifactEvidence(
          runtime.DB,
          artifact,
          normalizedFiles,
          rawKey,
          normalizedKeys,
          history.runId,
        );
        archivedPointerCount += evidenceCounts.pointerCount;
        archivedReadinessCount += evidenceCounts.readinessCount;
        manifestArtifacts.push({
          releaseId: artifact.releaseId,
          sourceId: artifact.sourceId,
          dataset: artifact.dataset,
          requestedDate: artifact.requestedDate,
          tradeDates: artifact.tradeDates,
          observedAt: artifact.observedAt,
          rawKey,
          metadataKey,
          normalizedKeys,
          stateKey,
        });
      } catch (error) {
        await writeCollectionState(runtime.RAW_BUCKET, {
          runId: history.runId,
          asOf: history.asOf,
          releaseId: artifact.releaseId,
          sourceId: artifact.sourceId,
          dataset: artifact.dataset,
          requestedDate: artifact.requestedDate,
          status: "FAILED",
          error: error instanceof Error ? error.message : "unknown_artifact_error",
        }).catch(() => undefined);
        throw error;
      }
    }

    if (sourceObservationR2Errors > 0) {
      return NextResponse.json(
        {
          error: "source_observation_receipt_failed",
          asOf: history.asOf,
          artifacts: history.artifacts.length,
          sourceObservationCount,
          sourceObservationD1Unavailable,
          sourceObservationR2Errors,
        },
        { status: 503 },
      );
    }

    // Non-final chunks only checkpoint their artifact.  Avoid the expensive
    // expected-set R2 scan and snapshot serialization until the final chunk,
    // where completeness is checked exactly once before atomic publication.
    if (!finalizeRequested && snapshot === undefined) {
      return NextResponse.json({
        status: "chunk_archived",
        asOf: history.asOf,
        artifacts: history.artifacts.length,
        standardizedRows,
        archivedPointerCount,
        archivedReadinessCount,
        sourceObservationCount,
        sourceObservationD1Unavailable,
      });
    }

    const collection = await checkCollectionComplete(
      runtime.RAW_BUCKET,
      history.runId,
      history.asOf,
      expectedArtifacts,
    );
    const shouldPublish = publishHistory && finalizeRequested && collection.complete;
    if (!snapshot) {
      return NextResponse.json({ error: "missing_final_snapshot" }, { status: 422 });
    }
    const snapshotBody = JSON.stringify(snapshot);
    const snapshotBytes = new TextEncoder().encode(snapshotBody);
    const snapshotSha256 = await sha256Hex(snapshotBytes);
    const historyPrefix = shouldPublish ? "market-data/history" : "market-data/history-pending";
    const manifestPrefix = shouldPublish
      ? "market-data/history-manifests"
      : "market-data/history-pending-manifests";
    const snapshotKey = `${historyPrefix}/${history.asOf}/${snapshotSha256}.json`;
    const manifestKey = `${manifestPrefix}/${history.runId}/${history.asOf}.json`;
    if (shouldPublish) {
      await runtime.RAW_BUCKET.put(snapshotKey, snapshotBytes, {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        customMetadata: { asOf: history.asOf, sha256: snapshotSha256 },
      });
    }
    await runtime.RAW_BUCKET.put(
      manifestKey,
      JSON.stringify({
        version: value.version,
        runId: history.runId,
        startedAt: history.startedAt,
        finishedAt: history.finishedAt,
        retentionDays: history.retentionDays,
        asOf: history.asOf,
        runKey: history.runKey ?? null,
        snapshotKey: shouldPublish ? snapshotKey : null,
        snapshotSha256,
        artifacts: manifestArtifacts,
        officialEtfDailyChanges: officialEtfDailyChanges ?? null,
        historyImportMode: archiveOnly ? "archive_only" : "legacy_compatibility",
        expectedArtifacts: expectedArtifacts ?? history.artifacts.map((artifact) => artifact.releaseId),
        collectionStateKeys,
        pendingArtifacts: collection.pending,
      }),
      { httpMetadata: { contentType: "application/json; charset=utf-8" } },
    );
    if (!shouldPublish) {
      return NextResponse.json({
        status: collection.complete ? "archived_pending" : "waiting_sources",
        asOf: history.asOf,
        historyImportMode: archiveOnly ? "archive_only" : "legacy_compatibility",
        artifacts: history.artifacts.length,
        standardizedRows,
        sourceObservationCount,
        sourceObservationD1Unavailable,
        snapshotSha256,
        snapshotKey: null,
        manifestKey,
        officialEtfDailyChanges: { status: "not_provided" },
        publication: snapshot.publication ?? null,
        ...(etfQualityIssues.length
          ? { reason: "derived_etf_quality_failed", qualityIssues: etfQualityIssues.slice(0, 50) }
          : {}),
        pendingArtifacts: collection.pending,
      });
    }
    const createdAt = new Date().toISOString();
    await runtime.DB
      .prepare(
        `INSERT INTO historical_snapshot (
           as_of, observed_at, snapshot_sha256, r2_object_key, manifest_key,
           source_count, quality_pass, quality_warn, quality_fail, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(as_of) DO UPDATE SET
           observed_at = excluded.observed_at,
           snapshot_sha256 = excluded.snapshot_sha256,
           r2_object_key = excluded.r2_object_key,
           manifest_key = excluded.manifest_key,
           source_count = excluded.source_count,
           quality_pass = excluded.quality_pass,
           quality_warn = excluded.quality_warn,
           quality_fail = excluded.quality_fail,
           created_at = excluded.created_at`,
      )
      .bind(
        history.asOf,
        snapshot.observedAt,
        snapshotSha256,
        snapshotKey,
        manifestKey,
        history.artifacts.length,
        snapshot.quality.pass,
        snapshot.quality.warn,
        snapshot.quality.fail,
        createdAt,
      )
      .run();

    const marketPoint = extractMarketHistoryPoint(snapshot);
    await runtime.DB
      .prepare(
        `INSERT INTO market_history_daily (
           trade_date, margin_balance, margin_change, financing_buy,
           lending_balance, lending_sell_volume, total_balance, sse_balance,
           szse_balance, southbound_net, southbound_turnover,
           southbound_sse_net, southbound_szse_net, northbound_turnover,
           northbound_etf_turnover, northbound_trade_count, margin_available,
           connect_available, source_snapshot_sha256, observed_at, created_at,
           bse_balance, margin_scope_version
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(trade_date) DO UPDATE SET
           margin_balance = excluded.margin_balance,
           margin_change = excluded.margin_change,
           financing_buy = excluded.financing_buy,
           lending_balance = excluded.lending_balance,
           lending_sell_volume = excluded.lending_sell_volume,
           total_balance = excluded.total_balance,
           sse_balance = excluded.sse_balance,
           szse_balance = excluded.szse_balance,
           southbound_net = excluded.southbound_net,
           southbound_turnover = excluded.southbound_turnover,
           southbound_sse_net = excluded.southbound_sse_net,
           southbound_szse_net = excluded.southbound_szse_net,
           northbound_turnover = excluded.northbound_turnover,
           northbound_etf_turnover = excluded.northbound_etf_turnover,
           northbound_trade_count = excluded.northbound_trade_count,
           margin_available = excluded.margin_available,
           connect_available = excluded.connect_available,
           source_snapshot_sha256 = excluded.source_snapshot_sha256,
           observed_at = excluded.observed_at,
           created_at = excluded.created_at,
           bse_balance = excluded.bse_balance,
           margin_scope_version = excluded.margin_scope_version`,
      )
      .bind(
        marketPoint.trade_date,
        marketPoint.margin_balance,
        marketPoint.margin_change,
        marketPoint.financing_buy,
        marketPoint.lending_balance,
        marketPoint.lending_sell_volume,
        marketPoint.total_balance,
        marketPoint.sse_balance,
        marketPoint.szse_balance,
        marketPoint.southbound_net,
        marketPoint.southbound_turnover,
        marketPoint.southbound_sse_net,
        marketPoint.southbound_szse_net,
        marketPoint.northbound_turnover,
        marketPoint.northbound_etf_turnover,
        marketPoint.northbound_trade_count,
        marketPoint.margin_available,
        marketPoint.connect_available,
        snapshotSha256,
        snapshot.observedAt,
        createdAt,
        marketPoint.bse_balance,
        marketPoint.margin_scope_version,
      )
      .run();

    const previousState = await loadEtfFlowState(runtime.RAW_BUCKET);
    // Keep the D1 query as a rollback path while the archive-only mode is
    // disabled.  Once explicitly enabled, the validated offline contract is
    // the only source for the ETF adjacent change used by the state merge.
    const officialDailyChanges = archiveOnly
      ? officialEtfDailyChanges
        ? officialDailyChangesFromContract(officialEtfDailyChanges)
        : []
      : await loadOfficialEtfDailyChanges(runtime.DB, history.asOf);
    const mergedState = mergeSnapshotIntoEtfFlowState(
      previousState,
      snapshot,
      snapshotSha256,
      officialDailyChanges,
    );
    const statePointer =
      mergedState && mergedState !== previousState
        ? await persistEtfFlowState(runtime.RAW_BUCKET, mergedState)
        : null;

    return NextResponse.json({
      status: "archived",
      asOf: history.asOf,
      historyImportMode: archiveOnly ? "archive_only" : "legacy_compatibility",
      artifacts: history.artifacts.length,
      standardizedRows,
      archivedPointerCount,
      archivedReadinessCount,
      sourceObservationCount,
      sourceObservationD1Unavailable,
      snapshotSha256,
      snapshotKey,
      manifestKey,
      marketHistory: {
        marginAvailable: Boolean(marketPoint.margin_available),
        connectAvailable: Boolean(marketPoint.connect_available),
      },
      etfFlowState: statePointer
        ? {
            asOf: statePointer.asOf,
            dateCount: statePointer.dateCount,
            sha256: statePointer.sha256,
          }
        : { status: "unchanged" },
      officialEtfDailyChanges: officialEtfDailyChanges
        ? {
            status: archiveOnly ? "applied_archive_only" : "validated_shadow_only",
            rows: officialEtfDailyChanges.rows.length,
            inputSha256: officialEtfDailyChanges.inputSha256,
          }
        : { status: "not_provided" },
    });
  } catch (error) {
    console.error("history import failed", error);
    return NextResponse.json(
      {
        error: "history_import_failed",
        detail: error instanceof Error ? error.message : "unknown_history_import_error",
      },
      { status: 500 },
    );
  }
}
