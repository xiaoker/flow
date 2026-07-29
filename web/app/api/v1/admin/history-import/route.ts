import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import type { DashboardSnapshot } from "@/app/lib/dashboard-data";
import {
  loadEtfFlowState,
  mergeSnapshotIntoEtfFlowState,
  persistEtfFlowState,
} from "@/app/lib/etf-flow-state";
import { extractMarketHistoryPoint } from "@/app/lib/market-history";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 12_000_000;
const MAX_ARTIFACTS = 16;
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
  dataset: "etf_scale" | "margin_market_daily" | "stock_connect_daily";
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
      dataset !== "margin_market_daily" &&
      dataset !== "stock_connect_daily")
  ) return false;
  const expectedPair =
    (dataset === "stock_connect_daily" && sourceId === "hkex") ||
    (dataset === "etf_scale" && (sourceId === "sse" || sourceId === "szse")) ||
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
      isDashboardSnapshot(history.snapshot) &&
      history.snapshot.asOf === history.asOf &&
      isPublishableSnapshot(history.snapshot)
  );
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
  if (!Number.isSafeInteger(value)) throw new Error(`invalid_integer:${field}`);
  return value;
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

async function persistStandardRows(
  db: D1Database,
  artifact: HistoryArtifact,
  normalizedFiles: Array<{ file: NormalizedFile; content: Uint8Array }>,
): Promise<number> {
  if (artifact.dataset === "stock_connect_daily") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "summary");
    if (!normalized) throw new Error("missing_connect_summary");
    const rows = parseCsv(normalized.content);
    if (rows.length !== normalized.file.rowCount) throw new Error("connect_row_count_mismatch");
    await db.prepare("DELETE FROM connect_daily WHERE release_id = ?").bind(artifact.releaseId).run();
    for (const row of rows) {
      validateStandardRow(artifact, row);
      if (!["SSE", "SZSE"].includes(row.market) || !["northbound", "southbound"].includes(row.direction)) {
        throw new Error("invalid_connect_channel");
      }
      await db
        .prepare(
          `INSERT INTO connect_daily (
             release_id, trade_date, market, direction, currency, total_turnover,
             buy_turnover, sell_turnover, net_buy, etf_turnover, source_id, observed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          artifact.releaseId,
          row.trade_date,
          row.market,
          row.direction,
          row.currency,
          requiredInteger(row, "total_turnover"),
          optionalInteger(row, "buy_turnover"),
          optionalInteger(row, "sell_turnover"),
          optionalInteger(row, "net_buy"),
          requiredInteger(row, "etf_turnover"),
          artifact.sourceId,
          row.observed_at,
        )
        .run();
    }
    return rows.length;
  }

  if (artifact.dataset === "margin_market_daily") {
    const normalized = normalizedFiles.find(({ file }) => file.name === "data");
    if (!normalized) throw new Error("missing_margin_data");
    const rows = parseCsv(normalized.content);
    if (rows.length !== normalized.file.rowCount) throw new Error("margin_row_count_mismatch");
    await db.prepare("DELETE FROM margin_daily WHERE release_id = ?").bind(artifact.releaseId).run();
    for (const row of rows) {
      validateStandardRow(artifact, row);
      if (!["SSE", "SZSE"].includes(row.market)) throw new Error("invalid_margin_market");
      await db
        .prepare(
          `INSERT INTO margin_daily (
             release_id, trade_date, market, financing_balance_yuan,
             financing_buy_yuan, securities_lending_balance_yuan,
             margin_total_balance_yuan, source_id, observed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          artifact.releaseId,
          row.trade_date,
          row.market,
          requiredInteger(row, "financing_balance_yuan"),
          requiredInteger(row, "financing_buy_yuan"),
          requiredInteger(row, "securities_lending_balance_yuan"),
          requiredInteger(row, "margin_total_balance_yuan"),
          artifact.sourceId,
          row.observed_at,
        )
        .run();
    }
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
    const manifestArtifacts: Array<Record<string, unknown>> = [];
    let standardizedRows = 0;
    for (const artifact of history.artifacts) {
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
      standardizedRows += await persistStandardRows(runtime.DB, artifact, normalizedFiles);
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
      });
    }

    const snapshotBody = JSON.stringify(history.snapshot);
    const snapshotBytes = new TextEncoder().encode(snapshotBody);
    const snapshotSha256 = await sha256Hex(snapshotBytes);
    const snapshotKey = `market-data/history/${history.asOf}/${snapshotSha256}.json`;
    const manifestKey = `market-data/history-manifests/${history.runId}/${history.asOf}.json`;
    await runtime.RAW_BUCKET.put(snapshotKey, snapshotBytes, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: { asOf: history.asOf, sha256: snapshotSha256 },
    });
    await runtime.RAW_BUCKET.put(
      manifestKey,
      JSON.stringify({
        version: value.version,
        runId: history.runId,
        startedAt: history.startedAt,
        finishedAt: history.finishedAt,
        retentionDays: history.retentionDays,
        asOf: history.asOf,
        snapshotKey,
        snapshotSha256,
        artifacts: manifestArtifacts,
      }),
      { httpMetadata: { contentType: "application/json; charset=utf-8" } },
    );
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

    const previousState = await loadEtfFlowState(runtime.RAW_BUCKET);
    const mergedState = mergeSnapshotIntoEtfFlowState(
      previousState,
      history.snapshot,
      snapshotSha256,
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
    return NextResponse.json({ error: "history_import_failed" }, { status: 500 });
  }
}
