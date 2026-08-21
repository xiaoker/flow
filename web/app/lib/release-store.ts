import { runtimeEnv } from "./dashboard-store";
import { flowBuildHeaders } from "./build-info";
import type { ReadModelSlot } from "./read-model-contract";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export type PublishedObject = {
  object: R2ObjectBody;
  asOf: string;
  releaseId: string;
  historical: boolean;
};

type DashboardReleaseRow = {
  id: string;
  as_of: string;
  r2_object_key: string;
};

type HistoricalReleaseRow = {
  as_of: string;
  r2_object_key: string;
};

type Runtime = Awaited<ReturnType<typeof runtimeEnv>>;

export const DEFAULT_RELEASE_SLOT: ReadModelSlot = "dashboard";

export type ActiveReleaseManifest = {
  releaseId: string;
  asOf: string;
  manifestSha256: string;
  observedAt: string | null;
  activatedAt: string;
  manifest: Record<string, unknown>;
};

async function readActiveReleaseManifest(
  db: D1Database,
  slot: ReadModelSlot = DEFAULT_RELEASE_SLOT,
): Promise<ActiveReleaseManifest | null> {
  const read = async (withObservedAt: boolean) => db
    .prepare(
      withObservedAt
        ? `SELECT active_release.release_id,
                  active_release.as_of,
                  active_release.manifest_sha256,
                  active_release.observed_at,
                  active_release.activated_at,
                  release_manifest.manifest_json
           FROM active_release
           JOIN release_manifest
             ON release_manifest.release_id = active_release.release_id
            AND release_manifest.as_of = active_release.as_of
            AND release_manifest.status = 'ACTIVE'
           WHERE active_release.slot = ?
           LIMIT 1`
        : `SELECT active_release.release_id,
                  active_release.as_of,
                  active_release.manifest_sha256,
                  active_release.activated_at,
                  release_manifest.manifest_json
           FROM active_release
           JOIN release_manifest
             ON release_manifest.release_id = active_release.release_id
            AND release_manifest.as_of = active_release.as_of
            AND release_manifest.status = 'ACTIVE'
           WHERE active_release.slot = ?
           LIMIT 1`,
    )
    .bind(slot)
    .first<{
      release_id: string;
      as_of: string;
      manifest_sha256: string;
      observed_at?: string | null;
      activated_at: string;
      manifest_json: string;
    }>();
  try {
    let row: Awaited<ReturnType<typeof read>>;
    try {
      row = await read(true);
    } catch {
      row = await read(false);
    }
    if (!row) return null;
    const manifest: unknown = JSON.parse(row.manifest_json);
    if (
      !manifest ||
      typeof manifest !== "object" ||
      Array.isArray(manifest) ||
      (manifest as Record<string, unknown>).releaseId !== row.release_id ||
      (manifest as Record<string, unknown>).asOf !== row.as_of ||
      ((manifest as Record<string, unknown>).slot ?? DEFAULT_RELEASE_SLOT) !== slot
    ) return null;
    return {
      releaseId: row.release_id,
      asOf: row.as_of,
      manifestSha256: row.manifest_sha256,
      observedAt: row.observed_at ??
        (typeof (manifest as Record<string, unknown>).observedAt === "string"
          ? (manifest as Record<string, unknown>).observedAt as string
          : null),
      activatedAt: row.activated_at,
      manifest: manifest as Record<string, unknown>,
    };
  } catch {
    // The pointer table is introduced after the existing snapshot tables;
    // legacy deployments safely fall back to dashboard_snapshot below.
    return null;
  }
}

export async function getActiveReleaseManifest(
  slot: ReadModelSlot = DEFAULT_RELEASE_SLOT,
): Promise<ActiveReleaseManifest | null> {
  const db = (await runtimeEnv()).DB;
  return db ? readActiveReleaseManifest(db, slot) : null;
}

/**
 * Resolve the immutable manifest for one published trading date. Historical
 * manifests may be SUPERSEDED in D1, but their R2 objects remain immutable and
 * must be selected by the requested date rather than falling back to today.
 */
export async function getReleaseManifestForDate(
  requestedDate?: string,
  slot: ReadModelSlot = DEFAULT_RELEASE_SLOT,
): Promise<ActiveReleaseManifest | null> {
  const db = (await runtimeEnv()).DB;
  if (!db) return null;
  if (requestedDate === undefined) return readActiveReleaseManifest(db, slot);
  if (!ISO_DATE.test(requestedDate)) return null;
  const active = await readActiveReleaseManifest(db, slot);
  if (active?.asOf === requestedDate) return active;
  try {
    const read = async (withObservedAt: boolean) => db
      .prepare(
        withObservedAt
          ? `SELECT release_id, as_of, manifest_sha256, observed_at, created_at, manifest_json
             FROM release_manifest
             WHERE as_of = ? AND status IN ('ACTIVE', 'SUPERSEDED')
               AND (json_extract(manifest_json, '$.slot') = ? OR (? = 'dashboard' AND json_extract(manifest_json, '$.slot') IS NULL))
             ORDER BY COALESCE(observed_at, json_extract(manifest_json, '$.observedAt'), '') DESC,
                      created_at DESC
             LIMIT 1`
          : `SELECT release_id, as_of, manifest_sha256, created_at, manifest_json
             FROM release_manifest
             WHERE as_of = ? AND status IN ('ACTIVE', 'SUPERSEDED')
               AND (json_extract(manifest_json, '$.slot') = ? OR (? = 'dashboard' AND json_extract(manifest_json, '$.slot') IS NULL))
             ORDER BY created_at DESC
             LIMIT 1`,
      )
      .bind(requestedDate, slot, slot)
      .first<{
        release_id: string;
        as_of: string;
        manifest_sha256: string;
        observed_at?: string | null;
        created_at: string;
        manifest_json: string;
      }>();
    let row: Awaited<ReturnType<typeof read>>;
    try {
      row = await read(true);
    } catch {
      row = await read(false);
    }
    if (!row) return null;
    const manifest: unknown = JSON.parse(row.manifest_json);
    if (
      !manifest ||
      typeof manifest !== "object" ||
      Array.isArray(manifest) ||
      (manifest as Record<string, unknown>).releaseId !== row.release_id ||
      (manifest as Record<string, unknown>).asOf !== row.as_of ||
      ((manifest as Record<string, unknown>).slot ?? DEFAULT_RELEASE_SLOT) !== slot
    ) return null;
    return {
      releaseId: row.release_id,
      asOf: row.as_of,
      manifestSha256: row.manifest_sha256,
      observedAt: row.observed_at ??
        (typeof (manifest as Record<string, unknown>).observedAt === "string"
          ? (manifest as Record<string, unknown>).observedAt as string
          : null),
      activatedAt: row.created_at,
      manifest: manifest as Record<string, unknown>,
    };
  } catch {
    return null;
  }
}

function bootstrapKeyFromManifest(active: ActiveReleaseManifest): string | null {
  const bootstrap = active.manifest?.artifacts;
  const bootstrapArtifact =
    bootstrap && typeof bootstrap === "object" && !Array.isArray(bootstrap)
      ? (bootstrap as Record<string, unknown>).bootstrap
      : null;
  const bootstrapKey =
    bootstrapArtifact && typeof bootstrapArtifact === "object" && !Array.isArray(bootstrapArtifact)
      ? (bootstrapArtifact as Record<string, unknown>).key
      : null;
  return typeof bootstrapKey === "string" ? bootstrapKey : null;
}

function artifactKeyFromManifest(active: ActiveReleaseManifest, name: string): string | null {
  const artifacts = active.manifest?.artifacts;
  const artifact =
    artifacts && typeof artifacts === "object" && !Array.isArray(artifacts)
      ? (artifacts as Record<string, unknown>)[name]
      : null;
  const key =
    artifact && typeof artifact === "object" && !Array.isArray(artifact)
      ? (artifact as Record<string, unknown>).key
      : null;
  return typeof key === "string" ? key : null;
}

async function readActivePublishedObject(
  runtime: Runtime,
  active: ActiveReleaseManifest,
  historical = false,
): Promise<PublishedObject | null> {
  if (!runtime.RAW_BUCKET) return null;
  const bootstrapKey = bootstrapKeyFromManifest(active);
  if (!bootstrapKey) return null;
  const object = await runtime.RAW_BUCKET.get(bootstrapKey);
  // An active pointer is authoritative. If its object is missing or carries
  // a different date/release, fail closed instead of serving an older
  // snapshot. This is also the safe path for an explicit `?date=` request
  // that names the currently active date.
  const releasePrefix = `market-data/releases/${active.asOf}/${active.releaseId}/`;
  if (
    !bootstrapKey.startsWith(releasePrefix) ||
    !object ||
    object.customMetadata?.asOf !== active.asOf ||
    object.customMetadata?.releaseId !== active.releaseId
  ) return null;
  return {
    object,
    asOf: active.asOf,
    releaseId: active.releaseId,
    historical,
  };
}

/**
 * Read one immutable release artifact without parsing it in the Worker. This
 * is used by stock search/detail routes so the browser, not the Worker, does
 * the small catalog lookup.
 */
export async function readPublishedArtifact(
  name: string,
  requestedDate?: string,
  slot: ReadModelSlot = DEFAULT_RELEASE_SLOT,
): Promise<PublishedObject | null> {
  const runtime = await runtimeEnv();
  if (!runtime.DB || !runtime.RAW_BUCKET) return null;
  const active = await getReleaseManifestForDate(requestedDate, slot);
  if (!active) return null;
  const key = artifactKeyFromManifest(active, name);
  const releasePrefix = `market-data/releases/${active.asOf}/${active.releaseId}/`;
  if (!key || !key.startsWith(releasePrefix)) return null;
  const object = await runtime.RAW_BUCKET.get(key);
  if (
    !object ||
    object.customMetadata?.asOf !== active.asOf ||
    object.customMetadata?.releaseId !== active.releaseId
  ) return null;
  return {
    object,
    asOf: active.asOf,
    releaseId: active.releaseId,
    historical: requestedDate !== undefined && requestedDate !== active.asOf,
  };
}

/**
 * Resolve a validated release pointer without reading its JSON body. The
 * caller can stream the R2 body directly to the browser, which keeps the
 * Worker out of the large-snapshot parsing path.
 */
export async function getPublishedObject(
  requestedDate?: string,
  slot: ReadModelSlot = DEFAULT_RELEASE_SLOT,
): Promise<PublishedObject | null> {
  const runtime = await runtimeEnv();
  if (!runtime.DB || !runtime.RAW_BUCKET) return null;
  if (requestedDate !== undefined && !ISO_DATE.test(requestedDate)) return null;

  try {

    let row: DashboardReleaseRow | HistoricalReleaseRow | null;
    let historical = false;
    const active = await readActiveReleaseManifest(runtime.DB, slot);

    // The latest date is represented by the active release, not by the
    // short historical snapshot created by the collection/archive path. A
    // browser may still retain `?date=<latest>` from an older UI version,
    // so resolve that URL to the same complete release as the date-less URL.
    if (requestedDate && active?.asOf === requestedDate) {
      return await readActivePublishedObject(runtime, active);
    }

    if (requestedDate) {
      // Prefer the immutable full release manifest. The compact historical
      // snapshot is retained only as a compatibility fallback for dates
      // published before release manifests were introduced.
      const dated = await getReleaseManifestForDate(requestedDate, slot);
      if (dated) {
        const published = await readActivePublishedObject(runtime, dated, dated.asOf !== active?.asOf);
        if (published) return published;
      }
      if (slot !== DEFAULT_RELEASE_SLOT) return null;
      historical = true;
      row = await runtime.DB
        .prepare(
          `SELECT as_of, r2_object_key
           FROM historical_snapshot
           WHERE as_of = ? AND quality_fail = 0
           LIMIT 1`,
        )
        .bind(requestedDate)
        .first<HistoricalReleaseRow>();
    } else {
      if (active) return await readActivePublishedObject(runtime, active);
      if (slot !== DEFAULT_RELEASE_SLOT) return null;
      row = await runtime.DB
        .prepare(
          `SELECT id, as_of, r2_object_key
           FROM dashboard_snapshot
           WHERE quality_fail = 0
           ORDER BY as_of DESC, published_at DESC
           LIMIT 1`,
        )
        .first<DashboardReleaseRow>();
    }
    if (!row) return null;

    const object = await runtime.RAW_BUCKET.get(row.r2_object_key);
    if (!object || object.customMetadata?.asOf !== row.as_of) return null;
    return {
      object,
      asOf: row.as_of,
      releaseId: "id" in row ? row.id : `historical:${row.as_of}`,
      historical,
    };
  } catch {
    return null;
  }
}

export function responseFromPublishedObject(
  published: PublishedObject,
  request: Request,
): Response {
  const etag = published.object.httpEtag;
  if (etag && request.headers.get("if-none-match") === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        "Cache-Control": "private, no-store, must-revalidate",
        ETag: etag,
        "X-Flow-As-Of": published.asOf,
        "X-Flow-Release-Id": published.releaseId,
        ...flowBuildHeaders(),
      },
    });
  }
  return new Response(published.object.body, {
    headers: {
      // A dashboard snapshot is selected by a mutable D1 release pointer.
      // Do not let the browser keep yesterday's pointer result after a new
      // collection has completed.
      "Cache-Control": "private, no-store, must-revalidate",
      "Content-Type": published.object.httpMetadata?.contentType ?? "application/json; charset=utf-8",
      ...(etag ? { ETag: etag } : {}),
      "X-Flow-As-Of": published.asOf,
      "X-Flow-Release-Id": published.releaseId,
      "X-Flow-Historical": String(published.historical),
      ...flowBuildHeaders(),
    },
  });
}
