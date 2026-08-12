import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  hasCompleteReadModelManifest,
  isReadModelManifest,
  type ReadModelManifest,
} from "@/app/lib/read-model-contract";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_CANDIDATES = 30;
const MAX_RETURNED_CANDIDATES = 10;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isCompleteCandidate(manifest: unknown): manifest is ReadModelManifest {
  return isReadModelManifest(manifest) && hasCompleteReadModelManifest(manifest);
}

export async function GET(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB) return Response.json({ error: "storage_unavailable" }, { status: 503 });

  const url = new URL(request.url);
  const before = url.searchParams.get("before") ?? "9999-12-31";
  const requestedLimit = Number(url.searchParams.get("limit") ?? "10");
  const limit = Number.isSafeInteger(requestedLimit)
    ? Math.min(MAX_RETURNED_CANDIDATES, Math.max(1, requestedLimit))
    : 10;
  if (!ISO_DATE.test(before)) return Response.json({ error: "invalid_before" }, { status: 400 });

  try {
    let rows: Array<{
      release_id: string;
      as_of: string;
      schema_version: number;
      observed_at?: string | null;
      created_at: string;
      manifest_json: string;
    }> = [];
    try {
      const result = await runtime.DB
        .prepare(
          `SELECT release_id, as_of, schema_version, observed_at, created_at, manifest_json
           FROM release_manifest
           WHERE as_of < ? AND status IN ('ACTIVE', 'SUPERSEDED')
           ORDER BY as_of DESC,
                    COALESCE(observed_at, json_extract(manifest_json, '$.observedAt'), '') DESC,
                    created_at DESC
           LIMIT ?`,
        )
        .bind(before, MAX_CANDIDATES)
        .all<typeof rows[number]>();
      rows = result.results ?? [];
    } catch {
      const result = await runtime.DB
        .prepare(
          `SELECT release_id, as_of, schema_version, created_at, manifest_json
           FROM release_manifest
           WHERE as_of < ? AND status IN ('ACTIVE', 'SUPERSEDED')
           ORDER BY as_of DESC, created_at DESC
           LIMIT ?`,
        )
        .bind(before, MAX_CANDIDATES)
        .all<typeof rows[number]>();
      rows = result.results ?? [];
    }

    const candidates = rows.flatMap((row) => {
      try {
        const parsed: unknown = JSON.parse(row.manifest_json);
        if (!isCompleteCandidate(parsed)) return [];
        return [{
          releaseId: row.release_id,
          asOf: row.as_of,
          observedAt: row.observed_at ?? parsed.observedAt ?? null,
          manifestSchemaVersion: row.schema_version,
          schemaVersion: row.schema_version,
          pressureSchemaVersion: parsed.pressureSchemaVersion ?? null,
          stockShardCount: parsed.stockShardCount ?? null,
          stockShardNames: Object.keys(parsed.artifacts).filter((name) => /^stock_[0-7]$/.test(name)),
          createdAt: row.created_at,
        }];
      } catch {
        return [];
      }
    }).slice(0, limit);

    return Response.json(
      {
        status: candidates.length ? "found" : "not_found",
        before,
        candidates,
        count: candidates.length,
      },
      { headers: { "Cache-Control": "private, no-store" } },
    );
  } catch (error) {
    console.error("previous release candidates failed", error);
    return Response.json({ error: "release_candidates_unavailable" }, { status: 503 });
  }
}
