import { NextResponse } from "next/server";
import { isDashboardSnapshot, runtimeEnv } from "@/app/lib/dashboard-store";
import { isPublishableSnapshot } from "@/app/lib/snapshot-policy.mjs";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 1_500_000;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function unauthorized() {
  return NextResponse.json({ error: "unauthorized" }, { status: 401 });
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const expectedKey = runtime.INGEST_API_KEY;
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!expectedKey || !suppliedKey || !secureEqual(suppliedKey, expectedKey)) {
    return unauthorized();
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return NextResponse.json({ error: "storage_unavailable" }, { status: 503 });
  }

  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }

  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!isDashboardSnapshot(payload)) {
    return NextResponse.json({ error: "invalid_snapshot" }, { status: 422 });
  }
  if (!isPublishableSnapshot(payload)) {
    return NextResponse.json(
      {
        error: "snapshot_not_publishable",
        gate: payload.gate,
        qualityFail: payload.quality.fail,
      },
      { status: 422 },
    );
  }

  const publishedAt = new Date().toISOString();
  const snapshotId = `${payload.asOf}:${crypto.randomUUID()}`;
  const objectKey = `dashboard/${payload.asOf}/${publishedAt.replaceAll(":", "-")}.json`;
  await runtime.RAW_BUCKET.put(objectKey, body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: { asOf: payload.asOf, gate: payload.gate },
  });
  await runtime.DB
    .prepare(
      `INSERT INTO dashboard_snapshot (
         id, as_of, observed_at, payload_json, published_at,
         quality_pass, quality_warn, quality_fail, r2_object_key
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .bind(
      snapshotId,
      payload.asOf,
      payload.observedAt,
      body,
      publishedAt,
      payload.quality.pass,
      payload.quality.warn,
      payload.quality.fail,
      objectKey,
    )
    .run();

  return NextResponse.json({
    status: "published",
    asOf: payload.asOf,
    publishedAt,
    snapshotId,
  });
}
