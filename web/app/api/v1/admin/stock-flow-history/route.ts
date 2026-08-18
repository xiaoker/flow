import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_BODY_BYTES = 5_000_000;
const DAILY_PREFIX = "market-data/derived/stock-flow-daily/v2";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function unauthorized(): Response {
  return Response.json({ error: "unauthorized" }, { status: 401 });
}

function isPartition(value: unknown): value is {
  schemaVersion: 2;
  partitionType: "etf_stock_flow_daily";
  available: true;
  asOf: string;
  items: Array<Record<string, unknown>>;
  coverage: Record<string, unknown>;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  return Boolean(
    candidate.schemaVersion === 2 &&
      candidate.partitionType === "etf_stock_flow_daily" &&
      candidate.available === true &&
      typeof candidate.asOf === "string" &&
      ISO_DATE.test(candidate.asOf) &&
      candidate.tradeDate === candidate.asOf &&
      (candidate.sharePreviousDate === null || typeof candidate.sharePreviousDate === "string") &&
      candidate.calculationVersion === "etf-stock-pressure-v2" &&
      Array.isArray(candidate.items) &&
      candidate.items.length > 0 &&
      candidate.items.length <= 100_000 &&
      candidate.items.every((item) => Boolean(item && typeof item === "object" && !Array.isArray(item))) &&
      candidate.coverage &&
      typeof candidate.coverage === "object" &&
      !Array.isArray(candidate.coverage),
  );
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", content);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function objectKey(asOf: string, sha256: string): string {
  return `${DAILY_PREFIX}/${asOf}/${sha256}.json`;
}

function pointerKey(asOf: string): string {
  return `${DAILY_PREFIX}/${asOf}/latest.json`;
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.RAW_BUCKET) return Response.json({ error: "raw_bucket_unavailable" }, { status: 503 });
  const asOf = new URL(request.url).searchParams.get("date") ?? "";
  if (!ISO_DATE.test(asOf)) return Response.json({ error: "invalid_date" }, { status: 400 });
  try {
    const pointer = await runtime.RAW_BUCKET.get(pointerKey(asOf));
    if (!pointer) return Response.json({ error: "partition_not_found", asOf }, { status: 404 });
    const pointerValue = JSON.parse(await pointer.text()) as { objectKey?: string; sha256?: string };
    if (
      typeof pointerValue.objectKey !== "string" ||
      !pointerValue.objectKey.startsWith(`${DAILY_PREFIX}/${asOf}/`) ||
      !/^[a-f0-9]{64}\.json$/.test(pointerValue.objectKey.split("/").at(-1) ?? "")
    ) {
      return Response.json({ error: "invalid_partition_pointer" }, { status: 500 });
    }
    const objectSha = pointerValue.objectKey.split("/").at(-1)?.replace(/\.json$/, "");
    if (pointerValue.sha256 !== objectSha) {
      return Response.json({ error: "invalid_partition_pointer" }, { status: 500 });
    }
    const object = await runtime.RAW_BUCKET.get(pointerValue.objectKey);
    if (!object?.body) return Response.json({ error: "partition_not_found", asOf }, { status: 404 });
    return new Response(object.body, {
      headers: {
        "Cache-Control": "private, no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Flow-Partition-As-Of": asOf,
        "X-Flow-Partition-Sha256": pointerValue.sha256 ?? "",
      },
    });
  } catch (error) {
    console.error("stock-flow history export failed", error);
    return Response.json({ error: "partition_export_failed" }, { status: 503 });
  }
}

export async function POST(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) return unauthorized();
  if (!runtime.RAW_BUCKET) return Response.json({ error: "raw_bucket_unavailable" }, { status: 503 });
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) return Response.json({ error: "payload_too_large" }, { status: 413 });
  const body = await request.text();
  const bytes = new TextEncoder().encode(body);
  if (bytes.byteLength > MAX_BODY_BYTES) return Response.json({ error: "payload_too_large" }, { status: 413 });
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return Response.json({ error: "invalid_json" }, { status: 400 });
  }
  const partition = value && typeof value === "object" && !Array.isArray(value) && "partition" in value
    ? (value as { partition?: unknown }).partition
    : value;
  if (!isPartition(partition)) return Response.json({ error: "invalid_partition" }, { status: 422 });
  try {
    const partitionBody = JSON.stringify(partition);
    const partitionBytes = new TextEncoder().encode(partitionBody);
    const sha256 = await sha256Hex(partitionBytes);
    const key = objectKey(partition.asOf, sha256);
    const existing = typeof runtime.RAW_BUCKET.head === "function" ? await runtime.RAW_BUCKET.head(key) : null;
    if (!existing) {
      await runtime.RAW_BUCKET.put(key, partitionBytes, {
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        customMetadata: { asOf: partition.asOf, sha256, schemaVersion: "2" },
      });
    }
    const pointer = JSON.stringify({ version: 1, asOf: partition.asOf, objectKey: key, sha256, updatedAt: new Date().toISOString() });
    await runtime.RAW_BUCKET.put(pointerKey(partition.asOf), pointer, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: { asOf: partition.asOf, sha256 },
    });
    return Response.json({ status: existing ? "already_archived" : "archived", asOf: partition.asOf, objectKey: key, sha256 });
  } catch (error) {
    console.error("stock-flow history import failed", error);
    return Response.json({ error: "partition_import_failed" }, { status: 500 });
  }
}
