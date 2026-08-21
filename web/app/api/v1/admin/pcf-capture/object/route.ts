import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const ETF_CODE = /^\d{6}$/;
const MAX_OBJECT_BYTES = 5 * 1024 * 1024;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function unauthorized(): Response {
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

function header(request: Request, name: string): string {
  return request.headers.get(name)?.trim() ?? "";
}

function isObjectKey(value: string, tradeDate: string, jobId: string, etfCode: string, sha256: string): boolean {
  return value === `market-data/pcf-v2/raw-xml/${tradeDate}/${jobId}/${etfCode}/${sha256}.xml.gz` &&
    !value.includes("..") && !value.includes("//");
}

export async function PUT(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB || !runtime.RAW_BUCKET) return Response.json({ error: "storage_unavailable" }, { status: 503 });
  if (!request.body) return Response.json({ error: "request_body_required" }, { status: 400 });

  const jobId = header(request, "X-Flow-Job-Id");
  const etfCode = header(request, "X-Flow-ETF-Code");
  const tradeDate = header(request, "X-Flow-Trade-Date");
  const objectKey = header(request, "X-Flow-Object-Key");
  const objectSha256 = header(request, "X-Flow-Object-Sha256").toLowerCase();
  const rawSha256 = header(request, "X-Flow-Raw-Sha256").toLowerCase();
  const contentLengthHeader = request.headers.get("content-length");
  const contentLength = Number(contentLengthHeader || 0);
  if (!jobId || !/^[A-Za-z0-9:_-]{8,120}$/.test(jobId) || !ETF_CODE.test(etfCode) || !ISO_DATE.test(tradeDate) ||
      !SHA256.test(objectSha256) || !SHA256.test(rawSha256) || !isObjectKey(objectKey, tradeDate, jobId, etfCode, objectSha256) ||
      !contentLengthHeader || !Number.isSafeInteger(contentLength) || contentLength <= 0 || contentLength > MAX_OBJECT_BYTES) {
    return Response.json({ error: "invalid_pcf_object_headers" }, { status: 422 });
  }

  try {
    const item = await runtime.DB.prepare(
      `SELECT job_id, etf_code, state FROM pcf_capture_item WHERE job_id = ? AND etf_code = ? LIMIT 1`,
    ).bind(jobId, etfCode).first<Record<string, unknown>>();
    if (!item) return Response.json({ error: "pcf_capture_item_not_found" }, { status: 404 });

    const existing = await runtime.RAW_BUCKET.head(objectKey);
    if (existing) {
      if (existing.customMetadata?.objectSha256 !== objectSha256 || existing.customMetadata?.rawSha256 !== rawSha256) {
        return Response.json({ error: "immutable_object_conflict" }, { status: 409 });
      }
      await runtime.DB.prepare(
        `UPDATE pcf_capture_item
            SET state = 'CAPTURED', raw_object_key = ?, raw_object_sha256 = ?, raw_content_sha256 = ?,
                raw_bytes = ?, updated_at = ?
          WHERE job_id = ? AND etf_code = ?`,
      ).bind(objectKey, objectSha256, rawSha256, existing.size, new Date().toISOString(), jobId, etfCode).run();
      await refreshJobCounts(runtime.DB, jobId);
      return Response.json({ key: objectKey, size: existing.size, idempotent: true, state: "CAPTURED" });
    }

    // Keep the body as a stream. Do not materialize, decode, or hash it here
    // in the Worker; the caller supplies the R2 object checksum and raw XML
    // checksum in headers, while the later offline finalize step verifies XML.
    const options = {
      httpMetadata: { contentType: "application/gzip", contentEncoding: "gzip" },
      customMetadata: {
        jobId,
        etfCode,
        tradeDate,
        objectSha256,
        rawSha256,
      },
      sha256: objectSha256,
    } as unknown as R2PutOptions;
    const stored = await runtime.RAW_BUCKET.put(objectKey, request.body, options);
    const size = stored?.size ?? contentLength;
    await runtime.DB.prepare(
      `UPDATE pcf_capture_item
          SET state = 'CAPTURED', raw_object_key = ?, raw_object_sha256 = ?, raw_content_sha256 = ?,
              raw_bytes = ?, updated_at = ?
        WHERE job_id = ? AND etf_code = ?`,
    ).bind(objectKey, objectSha256, rawSha256, size, new Date().toISOString(), jobId, etfCode).run();
    await refreshJobCounts(runtime.DB, jobId);
    return Response.json({ key: objectKey, size, idempotent: false, state: "CAPTURED" });
  } catch (error) {
    console.error("PCF capture object upload failed", error);
    return Response.json({ error: "pcf_object_upload_failed" }, { status: 503 });
  }
}

async function refreshJobCounts(db: D1Database, jobId: string): Promise<void> {
  const result = await db.prepare(
    `SELECT
       SUM(CASE WHEN state IN ('CAPTURED', 'VALID') THEN 1 ELSE 0 END) AS captured,
       SUM(CASE WHEN state = 'VALID' THEN 1 ELSE 0 END) AS valid,
       SUM(CASE WHEN state = 'BLOCKED' THEN 1 ELSE 0 END) AS failed
     FROM pcf_capture_item WHERE job_id = ?`,
  ).bind(jobId).first<Record<string, unknown>>();
  const captured = Number(result?.captured || 0);
  const valid = Number(result?.valid || 0);
  const failed = Number(result?.failed || 0);
  await db.prepare(
    `UPDATE pcf_capture_job
        SET captured_count = ?, valid_count = ?, failed_count = ?,
            coverage_ppm = CASE WHEN required_count = 0 THEN 1000000 ELSE CAST(? * 1000000 / required_count AS INTEGER) END,
            updated_at = ?
      WHERE job_id = ?`,
  ).bind(captured, valid, failed, valid, new Date().toISOString(), jobId).run();
}
