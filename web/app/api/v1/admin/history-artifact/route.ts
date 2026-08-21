import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ETF_SOURCE = /^(sse|szse)$/;
const DATASET = /^(etf_scale|etf_pcf)$/;
const FILE_NAME = /^[a-z0-9-]{1,24}$/;
const MAX_OBJECT_BYTES = 12_000_000;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function badRequest(error: string, status = 400): Response {
  return Response.json({ error }, { status });
}

function expectedPair(source: string, dataset: string): boolean {
  return dataset === "etf_scale" ? source === "sse" || source === "szse" : source === "sse";
}

function matchesFile(key: string, file: string): boolean {
  return key.endsWith(`/${file}.csv`) || key.endsWith(`/${file}.csv.gz`);
}

async function pointerFor(runtime: Awaited<ReturnType<typeof runtimeEnv>>, source: string, dataset: string, tradeDate: string, file: string) {
  if (!runtime.DB || source !== "sse" || dataset !== "etf_pcf") return null;
  try {
    return await runtime.DB.prepare(
      `SELECT revision_id AS revisionId, object_key AS objectKey, sha256, content_encoding AS contentEncoding,
              row_count AS rowCount, observed_at AS observedAt, status
         FROM history_artifact_pointer
        WHERE source_id = ? AND dataset = ? AND trade_date = ? AND file_name = ? AND status = 'READY'
        LIMIT 1`,
    ).bind(source, dataset, tradeDate, file).first<Record<string, unknown>>();
  } catch {
    // The additive pointer table may not exist on an older Worker schema.
    // Falling back preserves the legacy route until the migration is deployed.
    return null;
  }
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return badRequest("unauthorized", 401);
  }
  if (!runtime.RAW_BUCKET) return badRequest("raw_bucket_unavailable", 503);

  const query = new URL(request.url).searchParams;
  const tradeDate = query.get("date") ?? "";
  const source = query.get("source") ?? "";
  const dataset = query.get("dataset") ?? "";
  const file = query.get("file") ?? "data";
  if (!ISO_DATE.test(tradeDate)) return badRequest("invalid_date");
  if (!ETF_SOURCE.test(source) || !DATASET.test(dataset) || !FILE_NAME.test(file)) {
    return badRequest("invalid_artifact_identity");
  }
  if (!expectedPair(source, dataset)) return badRequest("unsupported_artifact_pair");

  const dateCandidates = [tradeDate, tradeDate.replaceAll("-", "")];
  try {
    const pointer = await pointerFor(runtime, source, dataset, tradeDate, file);
    if (pointer?.objectKey && typeof pointer.objectKey === "string") {
      const object = await runtime.RAW_BUCKET.get(pointer.objectKey);
      if (!object?.body) return badRequest("artifact_not_found", 404);
      const isGzip = pointer.objectKey.endsWith(".gz");
      return new Response(object.body, {
        headers: {
          "Cache-Control": "private, no-store",
          "Content-Type": isGzip ? "application/gzip" : "text/csv; charset=utf-8",
          "Content-Disposition": `attachment; filename="${source}-${dataset}-${tradeDate}-${file}${isGzip ? ".csv.gz" : ".csv"}"`,
          "X-Flow-Normalized-Key": pointer.objectKey,
          "X-Flow-Normalized-Sha256": String(pointer.sha256 ?? ""),
          "X-Flow-Trade-Date": tradeDate,
          "X-Flow-Pcf-Revision": String(pointer.revisionId ?? ""),
        },
      });
    }
    const objectsByKey = new Map<string, R2Object>();
    for (const requestedDate of dateCandidates) {
      const prefix = `market-data/normalized/${source}/${dataset}/${requestedDate}/`;
      let cursor: string | undefined;
      do {
        const listed = await runtime.RAW_BUCKET.list({ prefix, limit: 1000, cursor });
        for (const object of listed.objects ?? []) {
          if (matchesFile(object.key, file)) objectsByKey.set(object.key, object);
        }
        cursor = listed.truncated ? listed.cursor : undefined;
      } while (cursor);
    }
    const candidates = [...objectsByKey.values()];
    if (!candidates.length) return badRequest("artifact_not_found", 404);
    // A date can legitimately have more than one immutable object after a
    // corrected or retried official publication.  The normalized D1 tables
    // already resolve the same business key by latest observed_at; use the
    // same deterministic rule for historical ETF/PCF reconstruction.  The
    // selected key and candidate count are returned as lineage headers so a
    // partition never hides which immutable object was consumed.
    candidates.sort(
      (left, right) =>
        right.uploaded.getTime() - left.uploaded.getTime() ||
        left.key.localeCompare(right.key),
    );
    const selected = candidates[0];
    const key = selected.key;
    const object = await runtime.RAW_BUCKET.get(key);
    if (!object?.body) return badRequest("artifact_not_found", 404);
    if (typeof object.size === "number" && object.size > MAX_OBJECT_BYTES) {
      return badRequest("artifact_too_large", 413);
    }
    const isGzip = key.endsWith(".csv.gz");
    return new Response(object.body, {
      headers: {
        "Cache-Control": "private, no-store",
        "Content-Type": isGzip ? "application/gzip" : "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${source}-${dataset}-${tradeDate}-${file}${isGzip ? ".csv.gz" : ".csv"}"`,
        "X-Flow-Normalized-Key": key,
        "X-Flow-Normalized-Sha256": object.customMetadata?.sha256 ?? "",
        "X-Flow-Normalized-Candidate-Count": String(candidates.length),
        "X-Flow-Normalized-Selection": candidates.length > 1 ? "latest-uploaded" : "single",
        "X-Flow-Trade-Date": tradeDate,
      },
    });
  } catch (error) {
    console.error("history artifact export failed", error);
    return Response.json(
      { error: "history_artifact_export_failed" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
