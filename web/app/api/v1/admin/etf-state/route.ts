import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ETF_FLOW_STATE_POINTER_KEY =
  "market-data/derived/etf-flow-state/2026-07-v3/latest.json";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isStatePointer(value: unknown): value is {
  version: 1;
  classificationVersion: string;
  objectKey: string;
  sha256: string;
  asOf: string;
  dateCount: number;
  updatedAt: string;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const pointer = value as Record<string, unknown>;
  return Boolean(
    pointer.version === 1 &&
      typeof pointer.classificationVersion === "string" &&
      pointer.classificationVersion === "2026-07-v3" &&
      typeof pointer.objectKey === "string" &&
      pointer.objectKey.startsWith("market-data/derived/etf-flow-state/") &&
      typeof pointer.sha256 === "string" &&
      /^[a-f0-9]{64}$/.test(pointer.sha256) &&
      typeof pointer.asOf === "string" &&
      /^\d{4}-\d{2}-\d{2}$/.test(pointer.asOf) &&
      Number.isInteger(pointer.dateCount) &&
      Number(pointer.dateCount) > 0 &&
      typeof pointer.updatedAt === "string",
  );
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const expectedKey = runtime.INGEST_API_KEY;
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!expectedKey || !supplied || !secureEqual(supplied, expectedKey)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.RAW_BUCKET) {
    return Response.json({ error: "raw_bucket_unavailable" }, { status: 503 });
  }

  try {
    const pointerObject = await runtime.RAW_BUCKET.get(ETF_FLOW_STATE_POINTER_KEY);
    if (!pointerObject) {
      return Response.json({ error: "etf_state_pointer_not_found" }, { status: 404 });
    }
    const pointer = JSON.parse(await pointerObject.text()) as unknown;
    if (!isStatePointer(pointer)) {
      return Response.json({ error: "invalid_etf_state_pointer" }, { status: 500 });
    }
    const stateObject = await runtime.RAW_BUCKET.get(pointer.objectKey);
    if (!stateObject?.body) {
      return Response.json({ error: "etf_state_not_found" }, { status: 404 });
    }
    return new Response(stateObject.body, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-flow-state-as-of": pointer.asOf,
        "x-flow-state-sha256": pointer.sha256,
        "x-flow-state-date-count": String(pointer.dateCount),
      },
    });
  } catch (error) {
    console.error("etf state export failed", error);
    return Response.json(
      {
        error: "etf_state_export_failed",
        detail: error instanceof Error ? error.message : "unknown_etf_state_export_error",
      },
      { status: 500 },
    );
  }
}
