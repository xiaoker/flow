import { runtimeEnv } from "./dashboard-store";

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

export type ActiveReleaseManifest = {
  releaseId: string;
  asOf: string;
  manifestSha256: string;
  activatedAt: string;
  manifest: Record<string, unknown>;
};

async function readActiveReleaseManifest(db: D1Database): Promise<ActiveReleaseManifest | null> {
  try {
    const row = await db
      .prepare(
        `SELECT release_id, as_of, manifest_sha256, activated_at, manifest_json
         FROM active_release
         WHERE slot = 'dashboard'
         LIMIT 1`,
      )
      .first<{
        release_id: string;
        as_of: string;
        manifest_sha256: string;
        activated_at: string;
        manifest_json: string;
      }>();
    if (!row) return null;
    const manifest: unknown = JSON.parse(row.manifest_json);
    if (
      !manifest ||
      typeof manifest !== "object" ||
      Array.isArray(manifest) ||
      (manifest as Record<string, unknown>).releaseId !== row.release_id ||
      (manifest as Record<string, unknown>).asOf !== row.as_of
    ) return null;
    return {
      releaseId: row.release_id,
      asOf: row.as_of,
      manifestSha256: row.manifest_sha256,
      activatedAt: row.activated_at,
      manifest: manifest as Record<string, unknown>,
    };
  } catch {
    // The pointer table is introduced after the existing snapshot tables;
    // legacy deployments safely fall back to dashboard_snapshot below.
    return null;
  }
}

export async function getActiveReleaseManifest(): Promise<ActiveReleaseManifest | null> {
  const db = (await runtimeEnv()).DB;
  return db ? readActiveReleaseManifest(db) : null;
}

/**
 * Resolve a validated release pointer without reading its JSON body. The
 * caller can stream the R2 body directly to the browser, which keeps the
 * Worker out of the large-snapshot parsing path.
 */
export async function getPublishedObject(requestedDate?: string): Promise<PublishedObject | null> {
  const runtime = await runtimeEnv();
  if (!runtime.DB || !runtime.RAW_BUCKET) return null;
  if (requestedDate !== undefined && !ISO_DATE.test(requestedDate)) return null;

  try {

    let row: DashboardReleaseRow | HistoricalReleaseRow | null;
    let historical = false;
    if (requestedDate) {
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
      const active = await readActiveReleaseManifest(runtime.DB);
      const bootstrap = active?.manifest?.artifacts;
      const bootstrapArtifact =
        bootstrap && typeof bootstrap === "object" && !Array.isArray(bootstrap)
          ? (bootstrap as Record<string, unknown>).bootstrap
          : null;
      const bootstrapKey =
        bootstrapArtifact && typeof bootstrapArtifact === "object" && !Array.isArray(bootstrapArtifact)
          ? (bootstrapArtifact as Record<string, unknown>).key
          : null;
      if (active && typeof bootstrapKey === "string") {
        const object = await runtime.RAW_BUCKET.get(bootstrapKey);
        // An active pointer is authoritative. If its object is missing or
        // carries a different date, fail closed instead of silently showing
        // an older dashboard_snapshot and misleading the user.
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
          historical: false,
        };
      }
      if (active) return null;
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
    },
  });
}
