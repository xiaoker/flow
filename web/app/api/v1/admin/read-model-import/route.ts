import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import { activateReadModels, type ReadModelManifest } from "../import/route";

export const dynamic = "force-dynamic";

// Each request contains one compressed read model. Keeping this endpoint
// intentionally small avoids making the free Worker parse the full dashboard
// plus every view model in one invocation.
const MAX_BODY_BYTES = 1_500_000;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const RELEASE_ID = /^release:\d{4}-\d{2}-\d{2}:[a-f0-9]{16}$/;
const NAME = /^[a-z_]+$/;

type ReadModelArtifactUpload = {
  name: string;
  key: string;
  sha256: string;
  bytes: number;
  contentBase64: string;
  contentEncoding?: "gzip";
};

type ReadModelUpload = {
  version: 1;
  manifest: ReadModelManifest;
  artifact: ReadModelArtifactUpload;
  finalize?: boolean;
};

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isManifest(value: unknown): value is ReadModelManifest {
  if (!isRecord(value)) return false;
  if (
    value.schemaVersion !== 2 ||
    typeof value.releaseId !== "string" ||
    !RELEASE_ID.test(value.releaseId) ||
    typeof value.asOf !== "string" ||
    !ISO_DATE.test(value.asOf) ||
    !isRecord(value.artifacts)
  ) return false;
  const names = Object.keys(value.artifacts);
  if (!names.length || names.length > 8 || names.some((name) => !NAME.test(name))) return false;
  return names.every((name) => {
    const declared = value.artifacts[name];
    return Boolean(
      isRecord(declared) &&
        typeof declared.key === "string" &&
        declared.key.startsWith("market-data/releases/") &&
        !declared.key.includes("..") &&
        typeof declared.sha256 === "string" &&
        SHA256.test(declared.sha256) &&
        Number.isSafeInteger(declared.bytes) &&
        declared.bytes > 0 &&
        declared.bytes <= 5_000_000,
    );
  });
}

function isUpload(value: unknown): value is ReadModelUpload {
  if (!isRecord(value) || value.version !== 1 || !isManifest(value.manifest) || !isRecord(value.artifact)) {
    return false;
  }
  const artifact = value.artifact;
  const name = artifact.name;
  const declared = typeof name === "string" ? value.manifest.artifacts[name] : undefined;
  return Boolean(
    typeof name === "string" &&
      NAME.test(name) &&
      isRecord(declared) &&
      artifact.key === declared.key &&
      artifact.sha256 === declared.sha256 &&
      artifact.bytes === declared.bytes &&
      typeof artifact.contentBase64 === "string" &&
      artifact.contentBase64.length > 0 &&
      (artifact.contentEncoding === undefined || artifact.contentEncoding === "gzip") &&
      (value.finalize === undefined || typeof value.finalize === "boolean"),
  );
}

function decodeBase64(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("invalid_base64");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function decodeContent(artifact: ReadModelArtifactUpload): Promise<Uint8Array> {
  const encoded = decodeBase64(artifact.contentBase64);
  if (artifact.contentEncoding !== "gzip") return encoded;
  const stream = new Blob([encoded]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function sha256Hex(content: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", content);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function putImmutable(
  bucket: R2Bucket,
  key: string,
  content: Uint8Array | string,
  sha256: string,
  options: R2PutOptions,
): Promise<void> {
  const existing = typeof bucket.head === "function" ? await bucket.head(key) : null;
  if (existing) {
    if (existing.customMetadata?.sha256 !== sha256) throw new Error(`read_model_immutable_conflict:${key}`);
    return;
  }
  await bucket.put(key, content, options);
}

async function allArtifactsPresent(bucket: R2Bucket, manifest: ReadModelManifest): Promise<boolean> {
  if (typeof bucket.head !== "function") return true;
  const checks = await Promise.all(
    Object.values(manifest.artifacts).map(async (artifact) => Boolean(await bucket.head(artifact.key))),
  );
  return checks.every(Boolean);
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB || !runtime.RAW_BUCKET) {
    return NextResponse.json({ error: "storage_unavailable" }, { status: 503 });
  }
  const declaredSize = Number(request.headers.get("content-length") ?? "0");
  if (declaredSize > MAX_BODY_BYTES) return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return NextResponse.json({ error: "payload_too_large" }, { status: 413 });
  }
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  if (!isUpload(value)) return NextResponse.json({ error: "invalid_read_model_upload" }, { status: 422 });

  try {
    const content = await decodeContent(value.artifact);
    if (content.byteLength !== value.artifact.bytes) throw new Error("read_model_size_mismatch");
    if ((await sha256Hex(content)) !== value.artifact.sha256) throw new Error("read_model_sha256_mismatch");
    await putImmutable(runtime.RAW_BUCKET, value.artifact.key, content, value.artifact.sha256, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf,
        sha256: value.artifact.sha256,
      },
    });

    if (value.finalize !== true) {
      return NextResponse.json({ status: "staged", asOf: value.manifest.asOf, name: value.artifact.name });
    }
    if (!(await allArtifactsPresent(runtime.RAW_BUCKET, value.manifest))) {
      return NextResponse.json({ status: "waiting_artifacts", asOf: value.manifest.asOf }, { status: 409 });
    }
    const manifestBody = JSON.stringify(value.manifest);
    const manifestBodySha256 = await sha256Hex(new TextEncoder().encode(manifestBody));
    const manifestKey = `market-data/releases/${value.manifest.asOf}/${value.manifest.releaseId}/manifest.json`;
    await putImmutable(runtime.RAW_BUCKET, manifestKey, manifestBody, manifestBodySha256, {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
      customMetadata: {
        releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf,
        sha256: manifestBodySha256,
      },
    });
    const activatedAt = new Date().toISOString();
    const activation = await activateReadModels(
      runtime.DB,
      {
        releaseId: value.manifest.releaseId,
        asOf: value.manifest.asOf,
        manifestKey,
        manifestSha256: value.manifest.manifestSha256 ?? manifestBodySha256,
      },
      value.manifest,
      activatedAt,
    );
    return NextResponse.json({
      status: activation,
      asOf: value.manifest.asOf,
      releaseId: value.manifest.releaseId,
      manifestKey,
    });
  } catch (error) {
    console.error("read model import failed", error);
    return NextResponse.json(
      { error: "read_model_import_failed", detail: error instanceof Error ? error.message : "unknown_error" },
      { status: 500 },
    );
  }
}
