import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const JOB_ID = /^pcf:\d{4}-\d{2}-\d{2}:[0-9a-f]{16}$/;
const OWNER = /^[A-Za-z0-9:_-]{8,120}$/;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}
function bad(error: string, status = 400): Response {
  return Response.json({ error }, { status });
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) return bad("unauthorized", 401);
  if (!runtime.DB) return bad("database_unavailable", 503);
  let body: { action?: string; jobId?: string; owner?: string };
  try {
    body = await request.json() as { action?: string; jobId?: string; owner?: string };
  } catch {
    return bad("invalid_json");
  }
  const action = body.action || "acquire";
  const jobId = String(body.jobId || "");
  const owner = String(body.owner || "");
  if (!JOB_ID.test(jobId) || !OWNER.test(owner) || !["acquire", "release"].includes(action)) return bad("invalid_lease_request", 422);
  const now = new Date();
  const nowIso = now.toISOString();
  const expiresIso = new Date(now.getTime() + 10 * 60 * 1_000).toISOString();
  try {
    if (action === "release") {
      await runtime.DB.prepare(
        `UPDATE pcf_capture_job SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
          WHERE job_id = ? AND lease_owner = ?`,
      ).bind(nowIso, jobId, owner).run();
      return Response.json({ jobId, owner, released: true });
    }
    const result = await runtime.DB.prepare(
      `UPDATE pcf_capture_job
          SET lease_owner = ?, lease_expires_at = ?,
              status = CASE WHEN status IN ('PLANNED', 'PARTIAL', 'RATE_LIMITED', 'RUNNING') THEN 'RUNNING' ELSE status END,
              updated_at = ?
        WHERE job_id = ?
          AND status NOT IN ('READY', 'READY_NO_CHANGE', 'EXPIRED_EXACT_WINDOW', 'BLOCKED_QUALITY', 'BLOCKED_UNIVERSE_ANOMALY')
          AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ? OR lease_owner = ?)`,
    ).bind(owner, expiresIso, nowIso, jobId, nowIso, owner).run();
    const changes = Number((result.meta as { changes?: number } | undefined)?.changes || 0);
    if (changes !== 1) return bad("pcf_capture_lease_unavailable", 409);
    return Response.json({ jobId, owner, leaseExpiresAt: expiresIso, acquired: true });
  } catch (error) {
    console.error("PCF capture lease operation failed", error);
    return bad("pcf_capture_lease_failed", 503);
  }
}
