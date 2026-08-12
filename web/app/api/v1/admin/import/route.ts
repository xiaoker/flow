import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import { validateEtfTerminalCoverage } from "@/app/lib/etf-flow-state";
import {
  isReadModelManifest,
  MAX_READ_MODEL_ARTIFACTS,
  MAX_READ_MODEL_BYTES,
  READ_MODEL_NAME,
  SHA256,
  type ReadModelManifest,
} from "@/app/lib/read-model-contract";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 8_500_000;
const MAX_ARTIFACTS = 8;
const MAX_ETF_ROWS = 10_000;
const RETENTION_DAYS = 365;
const DASHBOARD_RETENTION_DAYS = 30;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

type EtfRow = {
  code: string;
  name: string;
  category: string | null;
  shares: number;
  navMicros: number | null;
  navDate?: string | null;
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
  readModels?: ReadModelBundle;
};

type ReadModelArtifact = {
  key: string;
  sha256: string;
  bytes: number;
  contentBase64: string;
  contentEncoding?: "gzip";
};

type ReadModelBundle = {
  manifest: {
    schemaVersion: number;
    releaseId: string;
    asOf: string;
    observedAt?: string | null;
    sourceDates?: Record<string, string | null>;
    pressureSchemaVersion?: number;
    stockShardCount?: number;
    requiredArtifacts?: string[];
    manifestSha256?: string;
    artifacts: ReadModelManifest["artifacts"];
  };
  artifacts: Record<string, ReadModelArtifact>;
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
      (value.navDate === undefined || value.navDate === null || (typeof value.navDate === "string" && ISO_DATE.test(value.navDate))) &&
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

function isReadModelBundle(value: unknown): value is ReadModelBundle {
  if (!isRecord(value) || !isRecord(value.manifest) || !isRecord(value.artifacts)) {
    return false;
  }
  const manifest = value.manifest as ReadModelManifest;
  if (!isReadModelManifest(manifest)) return false;
  const names = Object.keys(manifest.artifacts);
  const provided = Object.keys(value.artifacts);
  if (!names.length || names.length > MAX_READ_MODEL_ARTIFACTS || names.some((name) => !READ_MODEL_NAME.test(name))) return false;
  if (names.some((name) => !provided.includes(name)) || provided.some((name) => !names.includes(name))) {
    return false;
  }
  return names.every((name) => {
    const declared = manifest.artifacts[name];
    const artifact = value.artifacts[name];
    return Boolean(
      isRecord(declared) &&
        isRecord(artifact) &&
        typeof declared.key === "string" &&
        declared.key.startsWith("market-data/releases/") &&
        !declared.key.includes("..") &&
        typeof declared.sha256 === "string" &&
        SHA256.test(declared.sha256) &&
        Number.isSafeInteger(declared.bytes) &&
        declared.bytes > 0 &&
        declared.bytes <= MAX_READ_MODEL_BYTES &&
        artifact.key === declared.key &&
        artifact.sha256 === declared.sha256 &&
        artifact.bytes === declared.bytes &&
        typeof artifact.contentBase64 === "string" &&
        artifact.contentBase64.length > 0 &&
        (artifact.contentEncoding === undefined || artifact.contentEncoding === "gzip"),
    );
  });
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

async function decodeReadModelContent(artifact: ReadModelArtifact): Promise<Uint8Array> {
  const encoded = decodeBase64(artifact.contentBase64);
  if (artifact.contentEncoding !== "gzip") return encoded;
  const stream = new Blob([encoded]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", content);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function verifyImmutableReadModel(
  bucket: R2Bucket,
  key: string,
  bytes: number,
  sha256: string,
): Promise<void> {
  // R2 exposes head() in production. Keep the compatibility guard for the
  // lightweight test adapter, but never activate a release without checking
  // size and digest when the real bucket is available.
  if (typeof bucket.head !== "function") return;
  const object = await bucket.head(key);
  if (!object) throw new Error(`read_model_missing_after_upload:${key}`);
  if (object.size !== bytes) throw new Error(`read_model_size_after_upload:${key}`);
  if (object.customMetadata?.sha256 !== sha256) {
    throw new Error(`read_model_digest_after_upload:${key}`);
  }
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
                   category, shares, nav_micros, nav_date, date_basis, source_id, observed_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
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
                row.navDate,
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

async function archiveReadModels(
  runtime: { DB: D1Database; RAW_BUCKET: R2Bucket },
  bundle: ReadModelBundle,
): Promise<{ releaseId: string; asOf: string; manifestKey: string; manifestSha256: string }> {
  if (!isReadModelBundle(bundle)) throw new Error("invalid_read_model_bundle");
  const manifestBody = JSON.stringify(bundle.manifest);
  // `manifestSha256` is the hash of the canonical manifest before its own
  // digest field is added. Keep that stable release identity in D1; the R2
  // object additionally carries the body hash in custom metadata.
  const manifestSha256 = bundle.manifest.manifestSha256 ??
    await sha256Hex(new TextEncoder().encode(manifestBody));
  const manifestBodySha256 = await sha256Hex(new TextEncoder().encode(manifestBody));
  for (const artifact of Object.values(bundle.artifacts)) {
    const content = await decodeReadModelContent(artifact);
    if (content.byteLength !== artifact.bytes) throw new Error(`read_model_size_mismatch:${artifact.key}`);
    if ((await sha256Hex(content)) !== artifact.sha256) {
      throw new Error(`read_model_sha256_mismatch:${artifact.key}`);
    }
    await putImmutableRaw(runtime.RAW_BUCKET, artifact.key, content, artifact.sha256, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: bundle.manifest.releaseId,
        asOf: bundle.manifest.asOf,
        sha256: artifact.sha256,
      },
    });
    await verifyImmutableReadModel(runtime.RAW_BUCKET, artifact.key, artifact.bytes, artifact.sha256);
  }
  const manifestKey = `market-data/releases/${bundle.manifest.asOf}/${bundle.manifest.releaseId}/manifest.json`;
  await putImmutableRaw(
    runtime.RAW_BUCKET,
    manifestKey,
    manifestBody,
    manifestBodySha256,
    {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: bundle.manifest.releaseId,
        asOf: bundle.manifest.asOf,
        sha256: manifestBodySha256,
      },
    },
  );
  await verifyImmutableReadModel(
    runtime.RAW_BUCKET,
    manifestKey,
    new TextEncoder().encode(manifestBody).byteLength,
    manifestBodySha256,
  );
  return {
    releaseId: bundle.manifest.releaseId,
    asOf: bundle.manifest.asOf,
    manifestKey,
    manifestSha256,
  };
}

export async function activateReadModels(
  db: D1Database,
  release: { releaseId: string; asOf: string; manifestKey: string; manifestSha256: string },
  manifest: ReadModelManifest,
  activatedAt: string,
): Promise<"activated" | "staged_older_than_active" | "staged_stale_revision"> {
  // A delayed/retried collection can finish after a newer trading date has
  // already been published. The release pointer is a monotonic cursor: a
  // newer date may replace an older one, and a release for the same date may
  // correct it, but an older date must remain staged. Keep the guard in SQL
  // so concurrent imports cannot regress the pointer between a preflight read
  // and write.
  const observedAt = manifest.observedAt ?? null;
  let observedAtColumnsReady = false;
  try {
    await db.prepare("SELECT observed_at FROM release_manifest LIMIT 1").first();
    await db.prepare("SELECT observed_at FROM active_release LIMIT 1").first();
    observedAtColumnsReady = true;
  } catch {
    // The release pointer migration is deliberately deployable after the
    // Worker. Keep the v2 SQL path alive until both nullable columns exist.
  }

  if (!observedAtColumnsReady) {
    await db.batch([
      db
        .prepare(
          `INSERT INTO release_manifest (
             release_id, as_of, schema_version, manifest_sha256, manifest_json, status, created_at
           ) VALUES (?, ?, ?, ?, ?, 'STAGED', ?)
           ON CONFLICT(release_id) DO UPDATE SET
             as_of = excluded.as_of,
             schema_version = excluded.schema_version,
             manifest_sha256 = excluded.manifest_sha256,
             manifest_json = excluded.manifest_json,
             status = CASE
               WHEN release_manifest.status = 'ACTIVE' THEN 'ACTIVE'
               ELSE 'STAGED'
             END`,
        )
        .bind(
          release.releaseId,
          release.asOf,
          manifest.schemaVersion,
          release.manifestSha256,
          JSON.stringify(manifest),
          activatedAt,
        ),
      db
        .prepare(
          `UPDATE release_manifest
           SET status = 'SUPERSEDED'
           WHERE status = 'ACTIVE'
             AND release_id <> ?
             AND (
               NOT EXISTS (SELECT 1 FROM active_release WHERE slot = 'dashboard')
               OR EXISTS (
                 SELECT 1 FROM active_release
                 WHERE slot = 'dashboard' AND as_of <= ?
               )
             )`,
        )
        .bind(release.releaseId, release.asOf),
      db
        .prepare(
          `UPDATE release_manifest
           SET status = 'ACTIVE'
           WHERE release_id = ?
             AND (
               NOT EXISTS (SELECT 1 FROM active_release WHERE slot = 'dashboard')
               OR EXISTS (
                 SELECT 1 FROM active_release
                 WHERE slot = 'dashboard' AND as_of <= ?
               )
             )`,
        )
        .bind(release.releaseId, release.asOf),
      db
        .prepare(
          `INSERT INTO active_release (slot, release_id, as_of, manifest_sha256, activated_at)
           VALUES ('dashboard', ?, ?, ?, ?)
           ON CONFLICT(slot) DO UPDATE SET
             release_id = excluded.release_id,
             as_of = excluded.as_of,
             manifest_sha256 = excluded.manifest_sha256,
             activated_at = excluded.activated_at
           WHERE excluded.as_of >= active_release.as_of`,
        )
        .bind(release.releaseId, release.asOf, release.manifestSha256, activatedAt),
    ]);
  } else {
    // v3 makes same-day ordering explicit. A missing observation timestamp
    // (v2) is treated as older than any observed v3 release, never as "now".
    const newerOrSame = `
      active_release.as_of < ? OR (
        active_release.as_of = ? AND (
          COALESCE(active_release.observed_at, '') < COALESCE(?, '') OR
          (COALESCE(active_release.observed_at, '') = COALESCE(?, '') AND active_release.release_id = ?)
        )
      )`;
    await db.batch([
      db
        .prepare(
          `INSERT INTO release_manifest (
             release_id, as_of, schema_version, manifest_sha256, manifest_json,
             observed_at, status, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'STAGED', ?)
           ON CONFLICT(release_id) DO UPDATE SET
             as_of = excluded.as_of,
             schema_version = excluded.schema_version,
             manifest_sha256 = excluded.manifest_sha256,
             manifest_json = excluded.manifest_json,
             observed_at = excluded.observed_at,
             status = CASE
               WHEN release_manifest.status = 'ACTIVE' THEN 'ACTIVE'
               ELSE 'STAGED'
             END`,
        )
        .bind(
          release.releaseId,
          release.asOf,
          manifest.schemaVersion,
          release.manifestSha256,
          JSON.stringify(manifest),
          observedAt,
          activatedAt,
        ),
      db
        .prepare(
          `UPDATE release_manifest
           SET status = 'SUPERSEDED'
           WHERE status = 'ACTIVE'
             AND release_id <> ?
             AND (
               NOT EXISTS (SELECT 1 FROM active_release WHERE slot = 'dashboard')
               OR EXISTS (
                 SELECT 1 FROM active_release
                 WHERE slot = 'dashboard' AND ${newerOrSame}
               )
             )`,
        )
        .bind(release.releaseId, release.asOf, release.asOf, observedAt, observedAt, release.releaseId),
      db
        .prepare(
          `UPDATE release_manifest
           SET status = 'ACTIVE'
           WHERE release_id = ?
             AND (
               NOT EXISTS (SELECT 1 FROM active_release WHERE slot = 'dashboard')
               OR EXISTS (
                 SELECT 1 FROM active_release
                 WHERE slot = 'dashboard' AND ${newerOrSame}
               )
             )`,
        )
        .bind(release.releaseId, release.asOf, release.asOf, observedAt, observedAt, release.releaseId),
      db
        .prepare(
          `INSERT INTO active_release (
             slot, release_id, as_of, manifest_sha256, observed_at, activated_at
           ) VALUES ('dashboard', ?, ?, ?, ?, ?)
           ON CONFLICT(slot) DO UPDATE SET
             release_id = excluded.release_id,
             as_of = excluded.as_of,
             manifest_sha256 = excluded.manifest_sha256,
             observed_at = excluded.observed_at,
             activated_at = excluded.activated_at
           WHERE ${newerOrSame}`,
        )
        .bind(release.releaseId, release.asOf, release.manifestSha256, observedAt, activatedAt, release.asOf, release.asOf, observedAt, observedAt, release.releaseId),
    ]);
  }

  const active = await db
    .prepare(
      `SELECT release_id
       FROM active_release
       WHERE slot = 'dashboard'
       LIMIT 1`,
    )
    .first<{ release_id: string }>();
  return active?.release_id === release.releaseId
    ? "activated"
    : observedAtColumnsReady ? "staged_stale_revision" : "staged_older_than_active";
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
  if (payload.readModels && payload.readModels.manifest.asOf !== snapshot.asOf) {
    return NextResponse.json({ error: "read_model_snapshot_date_mismatch" }, { status: 422 });
  }

  // Retries of the same GitHub run must be idempotent. The run id is derived
  // from the source manifest and is stable across transport retries, so a
  // successful prior import can return without rewriting raw or dashboard
  // objects.
  try {
    const existingRun = await runtime.DB
      .prepare("SELECT status, manifest_key FROM ingest_run WHERE id = ? LIMIT 1")
      .bind(payload.archive.runId)
      .first<{ status: string; manifest_key: string | null }>();
    if (existingRun?.status === "SUCCESS") {
      return NextResponse.json({
        status: "already_published",
        asOf: snapshot.asOf,
        manifestKey: existingRun.manifest_key,
        idempotent: true,
      });
    }
  } catch (error) {
    console.warn("ingest idempotency preflight unavailable", error);
  }

  try {
    const archiveResult = await archiveEtfData(
      { DB: runtime.DB, RAW_BUCKET: runtime.RAW_BUCKET },
      payload,
    );
    const readModelResult = payload.readModels
      ? await archiveReadModels(
          { DB: runtime.DB, RAW_BUCKET: runtime.RAW_BUCKET },
          payload.readModels,
        )
      : null;
    if (!isPublishableSnapshot(snapshot)) {
      const finishedAt = new Date().toISOString();
      await runtime.DB
        .prepare(
          "UPDATE ingest_run SET finished_at = ?, status = 'PENDING', error_count = ?, manifest_key = ? WHERE id = ?",
        )
        .bind(
          finishedAt,
          Math.max(0, Number(snapshot.quality.fail) || 0),
          archiveResult.manifestKey,
          payload.archive.runId,
        )
        .run();
      return NextResponse.json({
        status: "archived_pending",
        asOf: snapshot.asOf,
        gate: snapshot.gate,
        qualityFail: snapshot.quality.fail,
        publication: snapshot.publication ?? null,
        ...archiveResult,
      });
    }
    // The collector must send a materialized terminal. Never load or replay
    // the large rolling R2 state in this request: doing so made publication
    // itself susceptible to the same Worker 1102 resource ceiling as reads.
    const publishedSnapshot = snapshot;
    const etfQualityIssues = validateEtfTerminalCoverage(publishedSnapshot.etfTerminal);
    if (etfQualityIssues.length) {
      const finishedAt = new Date().toISOString();
      await runtime.DB
        .prepare(
          "UPDATE ingest_run SET finished_at = ?, status = 'PENDING', error_count = ?, manifest_key = ? WHERE id = ?",
        )
        .bind(
          finishedAt,
          etfQualityIssues.length,
          archiveResult.manifestKey,
          payload.archive.runId,
        )
        .run();
      return NextResponse.json({
        status: "archived_pending",
        asOf: snapshot.asOf,
        reason: "derived_etf_quality_failed",
        qualityIssues: etfQualityIssues.slice(0, 50),
        ...archiveResult,
      });
    }
    const publishedAt = new Date().toISOString();
    const snapshotId = `${publishedSnapshot.asOf}:${crypto.randomUUID()}`;
    const objectKey = `dashboard/${publishedSnapshot.asOf}/${publishedAt.replaceAll(":", "-")}.json`;
    const snapshotBody = JSON.stringify(publishedSnapshot);
    const snapshotBytes = new TextEncoder().encode(snapshotBody).byteLength;
    await runtime.RAW_BUCKET.put(objectKey, snapshotBody, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        asOf: publishedSnapshot.asOf,
        gate: publishedSnapshot.gate,
        etfHistoryDays: String(publishedSnapshot.etfTerminal?.historyCoverage?.tradingDays ?? 0),
      },
    });
    // The legacy D1 fallback table is intentionally bounded. Full ETF
    // terminals are multi-megabyte release artifacts and exceed D1 binding
    // limits; v2 serves them directly from R2 through the active release
    // pointer. Keep small snapshots for old deployments, but never let a
    // large legacy write make an otherwise verified release fail.
    if (snapshotBytes <= 900_000) {
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
    } else {
      console.info("legacy dashboard_snapshot omitted for large R2 release", {
        asOf: publishedSnapshot.asOf,
        bytes: snapshotBytes,
      });
    }
    await runtime.DB
      .prepare(
        "UPDATE ingest_run SET finished_at = ?, status = 'SUCCESS', error_count = 0, manifest_key = ? WHERE id = ?",
      )
      .bind(publishedAt, archiveResult.manifestKey, payload.archive.runId)
      .run();
    let readModelActivation: "activated" | "staged_older_than_active" | "staged_stale_revision" | "unavailable" | null = null;
    if (readModelResult && payload.readModels) {
      try {
        readModelActivation = await activateReadModels(
          runtime.DB,
          readModelResult,
          payload.readModels.manifest,
          publishedAt,
        );
      } catch (pointerError) {
        // The release pointer migration can be applied independently of the
        // existing ingest tables. Keep the dashboard publish successful on a
        // legacy deployment; v2 falls back to dashboard_snapshot until the
        // migration is applied.
        console.error("read-model release pointer unavailable", pointerError);
        readModelActivation = "unavailable";
      }
    }

    return NextResponse.json({
      status: "published",
      asOf: publishedSnapshot.asOf,
      publishedAt,
      snapshotId,
      etfHistoryDays: publishedSnapshot.etfTerminal?.historyCoverage?.tradingDays ?? 0,
      readModelReleaseId: readModelResult?.releaseId ?? null,
      readModelActivation,
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
