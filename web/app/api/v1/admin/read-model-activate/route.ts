import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  hasCompleteReadModelManifest,
  isCandidateOnlyReadModelManifest,
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
  created_at: string;
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
  if (value.publishHistory !== undefined && typeof value.publishHistory !== "boolean") {
    return NextResponse.json({ error: "invalid_history_publication_intent" }, { status: 422 });
  }
  const publishHistory = value.publishHistory === true;
  const releaseId = typeof value.releaseId === "string" ? value.releaseId : "";
  const expectedAsOf = typeof value.asOf === "string" ? value.asOf : null;
  const expectedSlot = value.slot === undefined ? null : value.slot;
  if (expectedSlot !== null && expectedSlot !== "dashboard" && expectedSlot !== "etf_penetration") {
    return NextResponse.json({ error: "invalid_release_slot" }, { status: 400 });
  }
  if (!RELEASE_ID.test(releaseId)) {
    return NextResponse.json({ error: "invalid_release_id" }, { status: 400 });
  }

  try {
    const row = await runtime.DB
      .prepare(
        `SELECT release_id, as_of, manifest_sha256, manifest_json, status, created_at
           FROM release_manifest
          WHERE release_id = ? AND (status = 'STAGED' OR (? = 1 AND status IN ('SUPERSEDED','ACTIVE')))
          LIMIT 1`,
      )
      .bind(releaseId, publishHistory ? 1 : 0)
      .first<CandidateRow>();
    if (!row) return NextResponse.json({ error: "staged_release_not_found" }, { status: 404 });
    if (expectedAsOf && expectedAsOf !== row.as_of) {
      return NextResponse.json({ error: "release_date_mismatch" }, { status: 422 });
    }

    const parsed: unknown = JSON.parse(row.manifest_json);
    if (!isReadModelManifest(parsed) || !hasCompleteReadModelManifest(parsed)) {
      return NextResponse.json({ error: "invalid_staged_manifest" }, { status: 503 });
    }
    if (isCandidateOnlyReadModelManifest(parsed)) {
      return NextResponse.json({ error: "candidate_manifest_not_activatable" }, { status: 409 });
    }
    if (parsed.releaseId !== row.release_id || parsed.asOf !== row.as_of) {
      return NextResponse.json({ error: "staged_manifest_identity_mismatch" }, { status: 503 });
    }
    const parsedSlot = parsed.slot ?? "dashboard";
    if (expectedSlot !== null && expectedSlot !== parsedSlot) {
      return NextResponse.json({ error: "release_slot_mismatch" }, { status: 422 });
    }
    if (!(await candidateArtifactsAreIntact(runtime.RAW_BUCKET, parsed))) {
      return NextResponse.json({ error: "staged_artifact_unavailable" }, { status: 409 });
    }

    const activatedAt = new Date().toISOString();
    type Active = { release_id: string; as_of: string };
    const readActive = () => runtime.DB!.prepare(
      "SELECT release_id,as_of FROM active_release WHERE slot=? LIMIT 1",
    ).bind(parsedSlot).first<Active>();
    const activeBefore = publishHistory ? await readActive() : null;
    if (publishHistory && row.status === "ACTIVE" && activeBefore?.release_id === row.release_id
        && activeBefore.as_of === row.as_of) {
      return NextResponse.json({ status: "activated", releaseId: row.release_id, asOf: row.as_of,
        slot: parsedSlot, activeBeforeReleaseId: activeBefore.release_id,
        activeReleaseId: activeBefore.release_id, activeAsOf: activeBefore.as_of });
    }
    if (publishHistory && activeBefore && row.as_of < activeBefore.as_of) {
      // SUPERSEDED is the existing formally-readable historical state. This
      // explicit operation never writes active_release or activates v4.
      // Fence the one-row transition against the current pointer and newer
      // published revisions of this date. A concurrent change fails closed.
      const observedAt = parsed.observedAt ?? "";
      await runtime.DB.prepare(`UPDATE release_manifest SET status='SUPERSEDED'
        WHERE release_id=? AND as_of=? AND manifest_sha256=? AND status='STAGED'
          AND EXISTS (SELECT 1 FROM active_release WHERE slot=? AND release_id=? AND as_of>?)
          AND NOT EXISTS (SELECT 1 FROM release_manifest h
            WHERE h.as_of=? AND h.status IN ('ACTIVE','SUPERSEDED') AND h.release_id<>?
              AND (json_extract(h.manifest_json,'$.slot')=? OR (?='dashboard' AND json_extract(h.manifest_json,'$.slot') IS NULL))
              AND (COALESCE(h.observed_at,json_extract(h.manifest_json,'$.observedAt'),'')>?
                OR (COALESCE(h.observed_at,json_extract(h.manifest_json,'$.observedAt'),'')=? AND h.created_at>=?)))`)
        .bind(row.release_id, row.as_of, row.manifest_sha256, parsedSlot, activeBefore.release_id, row.as_of,
          row.as_of, row.release_id, parsedSlot, parsedSlot, observedAt, observedAt, row.created_at).run();
      const selected = await runtime.DB.prepare(`SELECT release_id FROM release_manifest
        WHERE as_of=? AND status IN ('ACTIVE','SUPERSEDED')
          AND (json_extract(manifest_json,'$.slot')=? OR (?='dashboard' AND json_extract(manifest_json,'$.slot') IS NULL))
        ORDER BY COALESCE(observed_at,json_extract(manifest_json,'$.observedAt'),'') DESC,created_at DESC LIMIT 1`)
        .bind(row.as_of, parsedSlot, parsedSlot).first<{ release_id: string }>();
      const activeAfter = await readActive();
      if (selected?.release_id !== row.release_id || activeAfter?.release_id !== activeBefore.release_id
          || activeAfter?.as_of !== activeBefore.as_of) {
        return NextResponse.json({ error: "historical_publication_conflict" }, { status: 409 });
      }
      return NextResponse.json({ status: "published_historical", releaseId: row.release_id, asOf: row.as_of,
        slot: parsedSlot, historyReadable: true, activeBeforeReleaseId: activeBefore.release_id,
        activeReleaseId: activeAfter.release_id, activeAsOf: activeAfter.as_of });
    }
    if (row.status !== "STAGED") {
      return NextResponse.json({ error: "historical_publication_requires_older_date" }, { status: 409 });
    }
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
    const activeAfter = publishHistory ? await readActive() : null;
    return NextResponse.json({ status, releaseId: row.release_id, asOf: row.as_of, slot: parsed.slot ?? "dashboard",
      ...(publishHistory ? { activeBeforeReleaseId: activeBefore?.release_id ?? null,
        activeReleaseId: activeAfter?.release_id ?? null, activeAsOf: activeAfter?.as_of ?? null } : {}) });
  } catch (error) {
    console.error("read model candidate activation failed", error);
    return NextResponse.json(
      { error: "read_model_activation_failed", detail: error instanceof Error ? error.message : "unknown_error" },
      { status: 500 },
    );
  }
}
