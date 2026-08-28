import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const ETF_CODE = /^\d{6}$/;
const MAX_OBJECT_BYTES = 5 * 1024 * 1024;
const CAPTURE_OUTCOMES = new Set(["EXACT", "DATE_MISMATCH", "QUALITY_FAILED"]);

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

function isObjectKey(value: string, prefix: "raw-xml" | "degraded", tradeDate: string, jobId: string, etfCode: string, sha256: string): boolean {
  return value === `market-data/pcf-v2/${prefix}/${tradeDate}/${jobId}/${etfCode}/${sha256}.xml.gz` &&
    !value.includes("..") && !value.includes("//");
}

function integerHeader(request: Request, name: string): number | null {
  const value = header(request, name);
  if (!value) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : Number.NaN;
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
  const outcome = header(request, "X-Flow-Capture-Outcome") || "EXACT";
  const sourceTradingDay = header(request, "X-Flow-Source-Trading-Day") || null;
  const sourcePreviousDay = header(request, "X-Flow-Source-Previous-Day") || null;
  const componentCount = integerHeader(request, "X-Flow-Component-Count");
  const normalizedSha256 = header(request, "X-Flow-Normalized-Sha256").toLowerCase() || null;
  const failureReason = header(request, "X-Flow-Failure-Reason").slice(0, 120) || null;
  const prefix = outcome === "EXACT" ? "raw-xml" : "degraded";
  const contentLengthHeader = request.headers.get("content-length");
  const contentLength = Number(contentLengthHeader || 0);
  if (!jobId || !/^[A-Za-z0-9:_-]{8,120}$/.test(jobId) || !ETF_CODE.test(etfCode) || !ISO_DATE.test(tradeDate) ||
      !CAPTURE_OUTCOMES.has(outcome) || (sourceTradingDay !== null && !ISO_DATE.test(sourceTradingDay)) ||
      (sourcePreviousDay !== null && !ISO_DATE.test(sourcePreviousDay)) || Number.isNaN(componentCount) ||
      (normalizedSha256 !== null && !SHA256.test(normalizedSha256)) ||
      (outcome === "EXACT" && (componentCount === null || normalizedSha256 === null)) ||
      !SHA256.test(objectSha256) || !SHA256.test(rawSha256) || !isObjectKey(objectKey, prefix, tradeDate, jobId, etfCode, objectSha256) ||
      !contentLengthHeader || !Number.isSafeInteger(contentLength) || contentLength <= 0 || contentLength > MAX_OBJECT_BYTES) {
    return Response.json({ error: "invalid_pcf_object_headers" }, { status: 422 });
  }

  try {
    const item = await runtime.DB.prepare(
      `SELECT item.job_id, item.etf_code, item.state, job.target_date, job.previous_date
         FROM pcf_capture_item item
         JOIN pcf_capture_job job ON job.job_id = item.job_id
        WHERE item.job_id = ? AND item.etf_code = ? LIMIT 1`,
    ).bind(jobId, etfCode).first<Record<string, unknown>>();
    if (!item) return Response.json({ error: "pcf_capture_item_not_found" }, { status: 404 });
    if (String(item.target_date) !== tradeDate) return Response.json({ error: "pcf_capture_date_mismatch" }, { status: 409 });
    if (outcome === "EXACT" && (sourceTradingDay !== tradeDate || sourcePreviousDay !== String(item.previous_date))) {
      return Response.json({ error: "pcf_capture_exact_date_contract_failed" }, { status: 409 });
    }

    const observedAt = new Date().toISOString();
    const state = outcome === "EXACT" ? "CAPTURED" : "BLOCKED";
    const recordCapture = async (size: number): Promise<void> => {
      await runtime.DB!.prepare(
        `UPDATE pcf_capture_item
            SET state = ?, raw_object_key = ?, raw_object_sha256 = ?, raw_content_sha256 = ?,
                raw_bytes = ?, source_trading_day = ?, source_previous_day = ?, component_count = ?,
                normalized_sha256 = ?, failure_reason = ?, observed_at = ?, attempts = attempts + 1, updated_at = ?
          WHERE job_id = ? AND etf_code = ?`,
      ).bind(
        state,
        objectKey,
        objectSha256,
        rawSha256,
        size,
        sourceTradingDay,
        sourcePreviousDay,
        componentCount,
        normalizedSha256,
        outcome === "EXACT" ? null : failureReason || outcome.toLowerCase(),
        observedAt,
        observedAt,
        jobId,
        etfCode,
      ).run();
      await refreshJobCounts(runtime.DB!, jobId);
      if (outcome === "DATE_MISMATCH") {
        const rolled = Boolean(sourceTradingDay && sourceTradingDay > tradeDate);
        await runtime.DB!.prepare(
          `UPDATE pcf_capture_job
              SET status = ?, error_json = ?, updated_at = ?
            WHERE job_id = ? AND status NOT IN ('READY', 'READY_NO_CHANGE')`,
        ).bind(
          rolled ? "EXPIRED_EXACT_WINDOW" : "PARTIAL",
          JSON.stringify({ outcome, etfCode, sourceTradingDay, sourcePreviousDay }),
          observedAt,
          jobId,
        ).run();
      } else if (outcome === "QUALITY_FAILED") {
        await runtime.DB!.prepare(
          `UPDATE pcf_capture_job
              SET status = 'BLOCKED_QUALITY', error_json = ?, updated_at = ?
            WHERE job_id = ? AND status NOT IN ('READY', 'READY_NO_CHANGE')`,
        ).bind(
          JSON.stringify({ outcome, etfCode, failureReason: failureReason || "quality_failed" }),
          observedAt,
          jobId,
        ).run();
      }
    };

    const existing = await runtime.RAW_BUCKET.head(objectKey);
    if (existing) {
      if (existing.customMetadata?.objectSha256 !== objectSha256 || existing.customMetadata?.rawSha256 !== rawSha256) {
        return Response.json({ error: "immutable_object_conflict" }, { status: 409 });
      }
      if (outcome === "EXACT" && existing.customMetadata?.normalizedSha256 !== normalizedSha256) {
        return Response.json({ error: "immutable_normalized_lineage_conflict" }, { status: 409 });
      }
      await recordCapture(existing.size);
      return Response.json({ key: objectKey, size: existing.size, idempotent: true, state, outcome });
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
        outcome,
        sourceTradingDay: sourceTradingDay || "",
        sourcePreviousDay: sourcePreviousDay || "",
        normalizedSha256: normalizedSha256 || "",
      },
      sha256: objectSha256,
    } as unknown as R2PutOptions;
    const stored = await runtime.RAW_BUCKET.put(objectKey, request.body, options);
    const size = stored?.size ?? contentLength;
    await recordCapture(size);
    return Response.json({ key: objectKey, size, idempotent: false, state, outcome });
  } catch (error) {
    console.error("PCF capture object upload failed", error);
    return Response.json({ error: "pcf_object_upload_failed" }, { status: 503 });
  }
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.DB || !runtime.RAW_BUCKET) return Response.json({ error: "storage_unavailable" }, { status: 503 });
  const query = new URL(request.url).searchParams;
  const jobId = query.get("jobId") ?? "";
  const etfCode = query.get("etfCode") ?? "";
  if (!/^[A-Za-z0-9:_-]{8,120}$/.test(jobId) || !ETF_CODE.test(etfCode)) {
    return Response.json({ error: "invalid_pcf_object_identity" }, { status: 400 });
  }
  try {
    const item = await runtime.DB.prepare(
      `SELECT state, raw_object_key, raw_object_sha256, raw_content_sha256, raw_bytes,
              source_trading_day, source_previous_day, normalized_sha256, observed_at
         FROM pcf_capture_item WHERE job_id = ? AND etf_code = ? LIMIT 1`,
    ).bind(jobId, etfCode).first<Record<string, unknown>>();
    if (!item || !["CAPTURED", "VALID"].includes(String(item.state)) || !item.raw_object_key) {
      return Response.json({ error: "pcf_exact_object_not_ready" }, { status: 404 });
    }
    const object = await runtime.RAW_BUCKET.get(String(item.raw_object_key));
    if (!object?.body) return Response.json({ error: "pcf_exact_object_missing" }, { status: 409 });
    if (object.size > MAX_OBJECT_BYTES || object.customMetadata?.objectSha256 !== item.raw_object_sha256 ||
        object.customMetadata?.rawSha256 !== item.raw_content_sha256) {
      return Response.json({ error: "pcf_exact_object_lineage_mismatch" }, { status: 409 });
    }
    return new Response(object.body, {
      headers: {
        "Cache-Control": "private, no-store",
        "Content-Type": "application/gzip",
        "Content-Length": String(object.size),
        "X-Flow-Object-Key": String(item.raw_object_key),
        "X-Flow-Object-Sha256": String(item.raw_object_sha256),
        "X-Flow-Raw-Sha256": String(item.raw_content_sha256),
        "X-Flow-Source-Trading-Day": String(item.source_trading_day || ""),
        "X-Flow-Source-Previous-Day": String(item.source_previous_day || ""),
        "X-Flow-Normalized-Sha256": String(item.normalized_sha256 || ""),
        "X-Flow-Observed-At": String(item.observed_at || ""),
      },
    });
  } catch (error) {
    console.error("PCF capture object download failed", error);
    return Response.json({ error: "pcf_object_download_failed" }, { status: 503 });
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
