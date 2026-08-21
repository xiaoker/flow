import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  hasCompleteReadModelManifest,
  isReadModelManifest,
  RELEASE_ID,
  type ReadModelManifest,
} from "@/app/lib/read-model-contract";
import { activateReadModels } from "../import/route";

export const dynamic = "force-dynamic";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

type CandidateRow = {
  release_id: string;
  as_of: string;
  manifest_sha256: string;
  manifest_json: string;
  status: string;
};

async function candidateArtifactsAreIntact(
  bucket: R2Bucket,
  manifest: ReadModelManifest,
): Promise<boolean> {
  const checks = await Promise.all(Object.entries(manifest.artifacts).map(async ([name, descriptor]) => {
    if (!descriptor.key.startsWith(`market-data/releases/${manifest.asOf}/${manifest.releaseId}/`)) return false;
    const object = await bucket.head(descriptor.key);
    return Boolean(
      object &&
        object.size === descriptor.bytes &&
        object.customMetadata?.asOf === manifest.asOf &&
        object.customMetadata?.releaseId === manifest.releaseId &&
        object.customMetadata?.sha256 === descriptor.sha256 &&
        name,
    );
  }));
  return checks.every(Boolean);
}

/**
 * Promote one already-canary-verified STAGED read-model release.
 *
 * Keeping activation separate from upload makes the production pointer an
 * explicit transaction boundary: a failed candidate canary cannot change what
 * normal readers serve, and a retry is idempotent by releaseId.
 */
export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return NextResponse.json({ error: "storage_unavailable" }, { status: 503 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "invalid_activation_request" }, { status: 422 });
  }
  const value = body as Record<string, unknown>;
  const releaseId = typeof value.releaseId === "string" ? value.releaseId : "";
  const expectedAsOf = typeof value.asOf === "string" ? value.asOf : null;
  if (!RELEASE_ID.test(releaseId)) {
    return NextResponse.json({ error: "invalid_release_id" }, { status: 400 });
  }

  try {
    const row = await runtime.DB
      .prepare(
        `SELECT release_id, as_of, manifest_sha256, manifest_json, status
           FROM release_manifest
          WHERE release_id = ? AND status = 'STAGED'
          LIMIT 1`,
      )
      .bind(releaseId)
      .first<CandidateRow>();
    if (!row) return NextResponse.json({ error: "staged_release_not_found" }, { status: 404 });
    if (expectedAsOf && expectedAsOf !== row.as_of) {
      return NextResponse.json({ error: "release_date_mismatch" }, { status: 422 });
    }

    const parsed: unknown = JSON.parse(row.manifest_json);
    if (!isReadModelManifest(parsed) || !hasCompleteReadModelManifest(parsed)) {
      return NextResponse.json({ error: "invalid_staged_manifest" }, { status: 503 });
    }
    if (parsed.releaseId !== row.release_id || parsed.asOf !== row.as_of) {
      return NextResponse.json({ error: "staged_manifest_identity_mismatch" }, { status: 503 });
    }
    if (!(await candidateArtifactsAreIntact(runtime.RAW_BUCKET, parsed))) {
      return NextResponse.json({ error: "staged_artifact_unavailable" }, { status: 409 });
    }

    const activatedAt = new Date().toISOString();
    const status = await activateReadModels(
      runtime.DB,
      {
        releaseId: row.release_id,
        asOf: row.as_of,
        manifestKey: `market-data/releases/${row.as_of}/${row.release_id}/manifest.json`,
        manifestSha256: row.manifest_sha256,
      },
      parsed,
      activatedAt,
    );
    return NextResponse.json({ status, releaseId: row.release_id, asOf: row.as_of });
  } catch (error) {
    console.error("read model candidate activation failed", error);
    return NextResponse.json(
      { error: "read_model_activation_failed", detail: error instanceof Error ? error.message : "unknown_error" },
      { status: 500 },
    );
  }
}
