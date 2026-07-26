import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import {
  enrichSnapshotWithEtfFlowState,
  loadEtfFlowState,
} from "@/app/lib/etf-flow-state";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 8_500_000;
const MAX_ARTIFACTS = 8;
const MAX_ETF_ROWS = 10_000;
const RETENTION_DAYS = 365;
const DASHBOARD_RETENTION_DAYS = 30;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;

type EtfRow = {
  code: string;
  name: string;
  category: string | null;
  shares: number;
  navMicros: number | null;
  dateBasis: string;
};

type ArchiveArtifact = {
  releaseId: string;
  sourceId: "sse" | "szse";
  market: "SSE" | "SZSE";
  tradeDate: string;
  observedAt: string;
  sourceUrl: string;
  dateBasis: string;
  sha256: string;
  contentType: string;
  extension: string;
  rawContentBase64: string;
  metadata: Record<string, unknown>;
  rows: EtfRow[];
};

type IngestBundle = {
  version: 1;
  snapshot: unknown;
  archive: {
    runId: string;
    startedAt: string;
    finishedAt: string;
    retentionDays: number;
    artifacts: ArchiveArtifact[];
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

function isEtfRow(value: unknown): value is EtfRow {
  if (!isRecord(value)) return false;
  return Boolean(
    typeof value.code === "string" &&
      /^\d{6}$/.test(value.code) &&
      typeof value.name === "string" &&
      value.name.length > 0 &&
      (value.category === null || typeof value.category === "string") &&
      Number.isSafeInteger(value.shares) &&
      value.shares >= 0 &&
      (value.navMicros === null ||
        (Number.isSafeInteger(value.navMicros) && value.navMicros >= 0)) &&
      typeof value.dateBasis === "string" &&
      value.dateBasis.length > 0,
  );
}

function isOfficialSourceUrl(sourceId: "sse" | "szse", value: string): boolean {
  try {
    const hostname = new URL(value).hostname.toLowerCase();
    return sourceId === "sse"
      ? hostname === "query.sse.com.cn"
      : hostname === "fund.szse.cn";
  } catch {
    return false;
  }
}

function isArchiveArtifact(value: unknown): value is ArchiveArtifact {
  if (!isRecord(value)) return false;
  const sourceId = value.sourceId;
  const market = value.market;
  return Boolean(
    (sourceId === "sse" || sourceId === "szse") &&
      (market === "SSE" || market === "SZSE") &&
      ((sourceId === "sse" && market === "SSE") ||
        (sourceId === "szse" && market === "SZSE")) &&
      typeof value.releaseId === "string" &&
      typeof value.tradeDate === "string" &&
      ISO_DATE.test(value.tradeDate) &&
      typeof value.observedAt === "string" &&
      value.observedAt.length > 0 &&
      typeof value.sourceUrl === "string" &&
      isOfficialSourceUrl(sourceId, value.sourceUrl) &&
      typeof value.dateBasis === "string" &&
      value.dateBasis.length > 0 &&
      typeof value.sha256 === "string" &&
      SHA256.test(value.sha256) &&
      value.releaseId ===
        `etf:${sourceId}:${value.tradeDate}:${value.sha256.slice(0, 16)}` &&
      typeof value.contentType === "string" &&
      value.contentType.length > 0 &&
      typeof value.extension === "string" &&
      /^[a-z0-9]{1,8}$/.test(value.extension) &&
      typeof value.rawContentBase64 === "string" &&
      value.rawContentBase64.length > 0 &&
      isRecord(value.metadata) &&
      value.metadata.sha256 === value.sha256 &&
      Array.isArray(value.rows) &&
      value.rows.length > 0 &&
      value.rows.length <= 2_500 &&
      value.rows.every(
        (row) => isEtfRow(row) && row.dateBasis === value.dateBasis,
      ),
  );
}

function isIngestBundle(value: unknown): value is IngestBundle {
  if (!isRecord(value) || value.version !== 1 || !isRecord(value.archive)) return false;
  const archive = value.archive;
  if (
    typeof archive.runId !== "string" ||
    !/^[A-Za-z0-9+:_-]{6,80}$/.test(archive.runId) ||
    typeof archive.startedAt !== "string" ||
    typeof archive.finishedAt !== "string" ||
    archive.retentionDays !== RETENTION_DAYS ||
    !Array.isArray(archive.artifacts) ||
    archive.artifacts.length === 0 ||
    archive.artifacts.length > MAX_ARTIFACTS ||
    !archive.artifacts.every(isArchiveArtifact)
  ) {
    return false;
  }
  return (
    archive.artifacts.reduce(
      (count, artifact) => count + (artifact as ArchiveArtifact).rows.length,
      0,
    ) <= MAX_ETF_ROWS
  );
}

function unauthorized() {
  return NextResponse.json({ error: "unauthorized" }, { status: 401 });
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("invalid_base64");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
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

function cutoffDate(tradeDates: string[], days: number): string {
  const latest = [...tradeDates].sort().at(-1);
  if (!latest) throw new Error("missing_trade_date");
  const cutoff = new Date(`${latest}T00:00:00Z`);
  cutoff.setUTCDate(cutoff.getUTCDate() - days);
  return cutoff.toISOString().slice(0, 10);
}

async function archiveEtfData(
  runtime: { DB: D1Database; RAW_BUCKET: R2Bucket },
  bundle: IngestBundle,
): Promise<{ archivedReleases: number; archivedRows: number; manifestKey: string }> {
  const { archive } = bundle;
  const prepared = await Promise.all(
    archive.artifacts.map(async (artifact) => {
      const content = decodeBase64(artifact.rawContentBase64);
      if ((await sha256Hex(content)) !== artifact.sha256) {
        throw new Error(`sha256_mismatch:${artifact.releaseId}`);
      }
      return { artifact, content };
    }),
  );
  const manifestKey = `market-data/manifests/${archive.runId}.json`;
  await runtime.DB
    .prepare(
      `INSERT INTO ingest_run (id, started_at, finished_at, status, error_count, manifest_key)
       VALUES (?, ?, NULL, 'RUNNING', 0, ?)
       ON CONFLICT(id) DO UPDATE SET
         started_at = excluded.started_at,
         finished_at = NULL,
         status = 'RUNNING',
         error_count = 0,
         manifest_key = excluded.manifest_key`,
    )
    .bind(archive.runId, archive.startedAt, manifestKey)
    .run();

  let archivedReleases = 0;
  let archivedRows = 0;
  try {
    const manifestArtifacts: Array<Record<string, unknown>> = [];
    for (const { artifact, content } of prepared) {
      const prefix = `market-data/raw/${artifact.sourceId}/etf_scale/${artifact.tradeDate}/${artifact.sha256}`;
      const rawKey = `${prefix}.${artifact.extension}`;
      const metadataKey = `${prefix}.metadata.json`;
      await putImmutableRaw(runtime.RAW_BUCKET, rawKey, content, artifact.sha256, {
        httpMetadata: { contentType: artifact.contentType },
        customMetadata: {
          sourceId: artifact.sourceId,
          tradeDate: artifact.tradeDate,
          sha256: artifact.sha256,
        },
      });
      await putImmutableRaw(
        runtime.RAW_BUCKET,
        metadataKey,
        JSON.stringify({
          ...artifact.metadata,
          release_id: artifact.releaseId,
          archived_raw_key: rawKey,
        }),
        artifact.sha256,
        {
          httpMetadata: { contentType: "application/json; charset=utf-8" },
          customMetadata: { sha256: artifact.sha256 },
        },
      );

      const existing = await runtime.DB
        .prepare("SELECT status, row_count FROM data_release WHERE id = ?")
        .bind(artifact.releaseId)
        .first<{ status: string; row_count: number }>();
      if (existing && existing.row_count !== artifact.rows.length) {
        throw new Error(`release_row_count_mismatch:${artifact.releaseId}`);
      }

      const domain = `etf:${artifact.market}:${artifact.tradeDate}`;
      if (!existing || existing.status !== "ACTIVE") {
        await runtime.DB.batch([
          runtime.DB.prepare("DELETE FROM etf_daily WHERE release_id = ?").bind(artifact.releaseId),
          runtime.DB
            .prepare(
              `INSERT INTO data_release (
                 id, domain, trade_date, status, source_id, ingest_run_id,
                 row_count, activated_at, created_at
               ) VALUES (?, ?, ?, 'STAGED', ?, ?, ?, NULL, ?)
               ON CONFLICT(id) DO UPDATE SET
                 domain = excluded.domain,
                 trade_date = excluded.trade_date,
                 status = 'STAGED',
                 source_id = excluded.source_id,
                 ingest_run_id = excluded.ingest_run_id,
                 row_count = excluded.row_count,
                 activated_at = NULL`,
            )
            .bind(
              artifact.releaseId,
              domain,
              artifact.tradeDate,
              artifact.sourceId,
              archive.runId,
              artifact.rows.length,
              archive.finishedAt,
            ),
        ]);
        for (let offset = 0; offset < artifact.rows.length; offset += 250) {
          const statements = artifact.rows.slice(offset, offset + 250).map((row) =>
            runtime.DB
              .prepare(
                `INSERT INTO etf_daily (
                   release_id, trade_date, market, instrument_code, instrument_name,
                   category, shares, nav_micros, date_basis, source_id, observed_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
              )
              .bind(
                artifact.releaseId,
                artifact.tradeDate,
                artifact.market,
                row.code,
                row.name,
                row.category,
                row.shares,
                row.navMicros,
                row.dateBasis,
                artifact.sourceId,
                artifact.observedAt,
              ),
          );
          await runtime.DB.batch(statements);
        }
        await runtime.DB.batch([
          runtime.DB
            .prepare(
              "UPDATE data_release SET status = 'SUPERSEDED' WHERE domain = ? AND status = 'ACTIVE' AND id <> ?",
            )
            .bind(domain, artifact.releaseId),
          runtime.DB
            .prepare(
              "UPDATE data_release SET status = 'ACTIVE', activated_at = ? WHERE id = ?",
            )
            .bind(archive.finishedAt, artifact.releaseId),
        ]);
      }
      archivedReleases += 1;
      archivedRows += artifact.rows.length;
      manifestArtifacts.push({
        releaseId: artifact.releaseId,
        sourceId: artifact.sourceId,
        market: artifact.market,
        tradeDate: artifact.tradeDate,
        observedAt: artifact.observedAt,
        sha256: artifact.sha256,
        rawKey,
        metadataKey,
        rowCount: artifact.rows.length,
      });
    }

    await runtime.RAW_BUCKET.put(
      manifestKey,
      JSON.stringify({
        version: bundle.version,
        runId: archive.runId,
        startedAt: archive.startedAt,
        finishedAt: archive.finishedAt,
        retentionDays: archive.retentionDays,
        artifacts: manifestArtifacts,
      }),
      { httpMetadata: { contentType: "application/json; charset=utf-8" } },
    );
    const tradeDates = archive.artifacts.map((artifact) => artifact.tradeDate);
    const cutoff = cutoffDate(tradeDates, RETENTION_DAYS);
    const dashboardCutoff = cutoffDate(tradeDates, DASHBOARD_RETENTION_DAYS);
    await runtime.DB.batch([
      runtime.DB
        .prepare(
          "DELETE FROM etf_daily WHERE release_id IN (SELECT id FROM data_release WHERE domain LIKE 'etf:%' AND trade_date < ?)",
        )
        .bind(cutoff),
      runtime.DB
        .prepare("DELETE FROM data_release WHERE domain LIKE 'etf:%' AND trade_date < ?")
        .bind(cutoff),
      runtime.DB
        .prepare("DELETE FROM dashboard_snapshot WHERE as_of < ?")
        .bind(dashboardCutoff),
    ]);
    return { archivedReleases, archivedRows, manifestKey };
  } catch (error) {
    await runtime.DB
      .prepare(
        "UPDATE ingest_run SET finished_at = ?, status = 'FAILED', error_count = 1 WHERE id = ?",
      )
      .bind(new Date().toISOString(), archive.runId)
      .run();
    throw error;
  }
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const expectedKey = runtime.INGEST_API_KEY;
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!expectedKey || !suppliedKey || !secureEqual(suppliedKey, expectedKey)) {
    return unauthorized();
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

  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!isIngestBundle(payload) || !isDashboardSnapshot(payload.snapshot)) {
    return NextResponse.json({ error: "invalid_ingest_bundle" }, { status: 422 });
  }
  const snapshot = payload.snapshot;
  if (!isPublishableSnapshot(snapshot)) {
    return NextResponse.json(
      {
        error: "snapshot_not_publishable",
        gate: snapshot.gate,
        qualityFail: snapshot.quality.fail,
      },
      { status: 422 },
    );
  }

  try {
    const archiveResult = await archiveEtfData(
      { DB: runtime.DB, RAW_BUCKET: runtime.RAW_BUCKET },
      payload,
    );
    const flowState = await loadEtfFlowState(runtime.RAW_BUCKET);
    const publishedSnapshot = enrichSnapshotWithEtfFlowState(snapshot, flowState);
    const publishedAt = new Date().toISOString();
    const snapshotId = `${publishedSnapshot.asOf}:${crypto.randomUUID()}`;
    const objectKey = `dashboard/${publishedSnapshot.asOf}/${publishedAt.replaceAll(":", "-")}.json`;
    const snapshotBody = JSON.stringify(publishedSnapshot);
    await runtime.RAW_BUCKET.put(objectKey, snapshotBody, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        asOf: publishedSnapshot.asOf,
        gate: publishedSnapshot.gate,
        etfFlowStateVersion: String(flowState?.version ?? 0),
      },
    });
    await runtime.DB
      .prepare(
        `INSERT INTO dashboard_snapshot (
           id, as_of, observed_at, payload_json, published_at,
           quality_pass, quality_warn, quality_fail, r2_object_key
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        snapshotId,
        publishedSnapshot.asOf,
        publishedSnapshot.observedAt,
        snapshotBody,
        publishedAt,
        publishedSnapshot.quality.pass,
        publishedSnapshot.quality.warn,
        publishedSnapshot.quality.fail,
        objectKey,
      )
      .run();
    await runtime.DB
      .prepare(
        "UPDATE ingest_run SET finished_at = ?, status = 'SUCCESS', error_count = 0, manifest_key = ? WHERE id = ?",
      )
      .bind(publishedAt, archiveResult.manifestKey, payload.archive.runId)
      .run();

    return NextResponse.json({
      status: "published",
      asOf: publishedSnapshot.asOf,
      publishedAt,
      snapshotId,
      etfFlowStateDates: flowState?.dates.length ?? 0,
      ...archiveResult,
    });
  } catch (error) {
    console.error("market data import failed", error);
    try {
      await runtime.DB
        .prepare(
          "UPDATE ingest_run SET finished_at = ?, status = 'FAILED', error_count = 1 WHERE id = ?",
        )
        .bind(new Date().toISOString(), payload.archive.runId)
        .run();
    } catch (statusError) {
      console.error("failed to mark ingest run as failed", statusError);
    }
    return NextResponse.json({ error: "import_failed" }, { status: 500 });
  }
}
