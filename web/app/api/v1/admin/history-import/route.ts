import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import type { DashboardSnapshot } from "@/app/lib/dashboard-data";
import {
  loadEtfFlowState,
  mergeSnapshotIntoEtfFlowState,
  persistEtfFlowState,
  type OfficialEtfDailyChange,
} from "@/app/lib/etf-flow-state";
import { extractMarketHistoryPoint } from "@/app/lib/market-history";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 12_000_000;
const MAX_ARTIFACTS = 16;
const MAX_EXPECTED_ARTIFACTS = 32;
const MAX_NORMALIZED_FILES = 2;
const HISTORY_RETENTION_DAYS = 0;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;

type NormalizedFile = {
  name: string;
  format: "csv";
  sha256: string;
  rowCount: number;
  tradeDates: string[];
  contentBase64: string;
};

type HistoryArtifact = {
  releaseId: string;
  sourceId: "sse" | "szse" | "hkex";
  dataset: "etf_scale" | "etf_nav" | "margin_market_daily" | "stock_connect_daily";
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

type HistoryBundle = {
  version: 1;
  history: {
    runId: string;
    startedAt: string;
    finishedAt: string;
    retentionDays: number;
    asOf: string;
    snapshot: DashboardSnapshot;
    artifacts: HistoryArtifact[];
    /**
     * Chunked collection contract.  Each request may carry one artifact, and
     * the final request sets finalize=true.  The expected ids are persisted in
     * R2 state so a missing exchange/date cannot be mistaken for a complete
     * publication.
     */
    expectedArtifacts?: string[];
    finalize?: boolean;
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
      typeof value.contentBase64 === "string",
  );
}

function isHistoryArtifact(value: unknown): value is HistoryArtifact {
  if (!isRecord(value)) return false;
  const sourceId = value.sourceId;
  const dataset = value.dataset;
  if (
    (sourceId !== "sse" && sourceId !== "szse" && sourceId !== "hkex") ||
    (dataset !== "etf_scale" &&
      dataset !== "etf_nav" &&
      dataset !== "margin_market_daily" &&
      dataset !== "stock_connect_daily")
  ) return false;
  const expectedPair =
    (dataset === "stock_connect_daily" && sourceId === "hkex") ||
    (dataset === "etf_scale" && (sourceId === "sse" || sourceId === "szse")) ||
    (dataset === "etf_nav" && sourceId === "sse") ||
    (dataset === "margin_market_daily" && (sourceId === "sse" || sourceId === "szse"));
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

function isHistoryBundle(value: unknown): value is HistoryBundle {
  if (!isRecord(value) || value.version !== 1 || !isRecord(value.history)) return false;
  const history = value.history;
  const expectedArtifacts = history.expectedArtifacts;
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
      expectedArtifactsValid &&
      (history.finalize === undefined || typeof history.finalize === "boolean") &&
      isDashboardSnapshot(history.snapshot) &&
      history.snapshot.asOf === history.asOf
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

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("invalid_base64");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
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
                ROW_NUMBER() OVER (
                  PARTITION BY instrument_code, market
                  ORDER BY trade_date DESC, observed_at DESC, release_id DESC
                ) AS row_number
         FROM etf_daily
         WHERE trade_date < ?
       )
       SELECT current_rows.instrument_code, current_rows.market,
              current_rows.shares AS current_shares,
              previous_rows.shares AS previous_shares
       FROM current_rows
       LEFT JOIN previous_rows
         ON previous_rows.instrument_code = current_rows.instrument_code
        AND previous_rows.market = current_rows.market
        AND previous_rows.row_number = 1
       WHERE current_rows.row_number = 1`,
    )
    .bind(asOf, asOf)
    .all<{
      instrument_code: string;
      market: "SSE" | "SZSE";
      current_shares: number;
      previous_shares: number | null;
    }>();
  return (result.results ?? []).flatMap((row) => {
    if ((row.market !== "SSE" && row.market !== "SZSE") || !/^\d{6}$/.test(row.instrument_code)) return [];
    return [{
      code: row.instrument_code,
      market: row.market,
      value: row.previous_shares === null
        ? null
        : Number(((row.current_shares - row.previous_shares) / 100_000_000).toFixed(6)),
    }];
  });
}

async function persistStandardRows(
  db: D1Database,
  artifact: HistoryArtifact,
  normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array }>,
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
      if (!["SSE", "SZSE"].includes(row.market)) throw new Error("invalid_margin_market");
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

  try {
    const { history } = value;
    const publishHistory = isPublishableSnapshot(history.snapshot);
    const finalizeRequested = history.finalize !== false;
    const expectedArtifacts = history.expectedArtifacts;
    const manifestArtifacts: Array<Record<string, unknown>> = [];
    const collectionStateKeys: string[] = [];
    let standardizedRows = 0;
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
        const normalizedKeys: string[] = [];
        const normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array }> = [];
        for (const normalized of artifact.normalized) {
          const content = decodeBase64(normalized.contentBase64);
          if ((await sha256Hex(content)) !== normalized.sha256) {
            throw new Error(`normalized_sha256_mismatch:${artifact.releaseId}:${normalized.name}`);
          }
          const key = `market-data/normalized/${artifact.sourceId}/${artifact.dataset}/${artifact.requestedDate}/${artifact.sha256}/${normalized.name}.csv`;
          await runtime.RAW_BUCKET.put(key, content, {
            httpMetadata: { contentType: "text/csv; charset=utf-8" },
            customMetadata: {
              sourceId: artifact.sourceId,
              dataset: artifact.dataset,
              rowCount: String(normalized.rowCount),
              sha256: normalized.sha256,
            },
          });
          normalizedKeys.push(key);
          normalizedFiles.push({ file: normalized, content });
        }
        const normalizedRowCount = normalizedFiles.reduce(
          (sum, item) => sum + item.file.rowCount,
          0,
        );
        // NAV is an optional enrichment feed.  An exchange may return an
        // empty exact-date list because its public page only retains the
        // latest bulletin; archive that evidence as VERIFIED so an empty NAV
        // feed cannot block the complete ETF share publication.
        if (normalizedRowCount === 0 && artifact.dataset !== "etf_nav") {
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
          standardizedRows += await persistStandardRows(runtime.DB, artifact, normalizedFiles);
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

    const collection = await checkCollectionComplete(
      runtime.RAW_BUCKET,
      history.runId,
      history.asOf,
      expectedArtifacts,
    );
    const shouldPublish = publishHistory && finalizeRequested && collection.complete;
    const snapshotBody = JSON.stringify(history.snapshot);
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
        snapshotKey: shouldPublish ? snapshotKey : null,
        snapshotSha256,
        artifacts: manifestArtifacts,
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
        artifacts: history.artifacts.length,
        standardizedRows,
        snapshotSha256,
        snapshotKey: null,
        manifestKey,
        publication: history.snapshot.publication ?? null,
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
        history.snapshot.observedAt,
        snapshotSha256,
        snapshotKey,
        manifestKey,
        history.artifacts.length,
        history.snapshot.quality.pass,
        history.snapshot.quality.warn,
        history.snapshot.quality.fail,
        createdAt,
      )
      .run();

    const marketPoint = extractMarketHistoryPoint(history.snapshot);
    await runtime.DB
      .prepare(
        `INSERT INTO market_history_daily (
           trade_date, margin_balance, margin_change, financing_buy,
           lending_balance, lending_sell_volume, total_balance, sse_balance,
           szse_balance, southbound_net, southbound_turnover,
           southbound_sse_net, southbound_szse_net, northbound_turnover,
           northbound_etf_turnover, northbound_trade_count, margin_available,
           connect_available, source_snapshot_sha256, observed_at, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
           created_at = excluded.created_at`,
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
        history.snapshot.observedAt,
        createdAt,
      )
      .run();

    const [previousState, officialDailyChanges] = await Promise.all([
      loadEtfFlowState(runtime.RAW_BUCKET),
      loadOfficialEtfDailyChanges(runtime.DB, history.asOf),
    ]);
    const mergedState = mergeSnapshotIntoEtfFlowState(
      previousState,
      history.snapshot,
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
      artifacts: history.artifacts.length,
      standardizedRows,
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
