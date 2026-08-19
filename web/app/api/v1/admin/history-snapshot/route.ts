import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_OBJECT_BYTES = 12_000_000;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function response(error: string, status: number): Response {
  return Response.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}

/**
 * Stream one quality-passed immutable historical snapshot for the read-only
 * recovery drill. The Worker performs the R2 read with its binding, so the
 * GitHub Actions Cloudflare token does not need direct R2 object permission.
 * No object is parsed, rewritten, or promoted by this endpoint.
 */
export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return response("unauthorized", 401);
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) return response("storage_unavailable", 503);

  const asOf = new URL(request.url).searchParams.get("date") ?? "";
  if (!ISO_DATE.test(asOf)) return response("invalid_date", 400);

  try {
    const row = await runtime.DB
      .prepare(
        `SELECT as_of, snapshot_sha256, r2_object_key
         FROM historical_snapshot
         WHERE as_of = ? AND quality_fail = 0
         LIMIT 1`,
      )
      .bind(asOf)
      .first<{ as_of: string; snapshot_sha256: string; r2_object_key: string }>();
    if (!row || row.as_of !== asOf || !row.snapshot_sha256 || !row.r2_object_key) {
      return response("snapshot_not_found", 404);
    }

    const object = await runtime.RAW_BUCKET.get(row.r2_object_key);
    if (!object?.body) return response("snapshot_not_found", 404);
    if (typeof object.size === "number" && object.size > MAX_OBJECT_BYTES) {
      return response("snapshot_too_large", 413);
    }

    return new Response(object.body, {
      headers: {
        "Cache-Control": "private, no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Flow-History-Snapshot-Sha256": row.snapshot_sha256,
        "X-Flow-Trade-Date": asOf,
      },
    });
  } catch (error) {
    console.error("history snapshot export failed", error);
    return response("snapshot_export_failed", 503);
  }
}
