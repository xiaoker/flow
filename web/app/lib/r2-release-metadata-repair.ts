import {
  ISO_DATE,
  READ_MODEL_NAME,
  RELEASE_ID,
  SHA256,
  isReadModelManifest,
  type ReadModelManifest,
} from "./read-model-contract.ts";

/**
 * Bounded, idempotent repair for legacy release artifacts whose bytes are
 * intact but whose R2 customMetadata.sha256 was not written by the original
 * publisher.
 *
 * This module deliberately operates on one manifest descriptor at a time.
 * It never changes D1, release status, active pointers, or any application
 * feature flag. The HTTP route is additionally disabled unless the explicit
 * R2_METADATA_REPAIR_ENABLED deployment variable is set to "1".
 */

export const R2_RELEASE_METADATA_REPAIR_CONTRACT = "r2-release-metadata-repair-v1" as const;

const MAX_OBJECT_BYTES = 8_000_000;
const MAX_INPUT_BYTES = 16_384;

type D1Row = {
  as_of: string;
  release_id: string;
  status: string;
  manifest_json: string;
};

type RepairD1 = {
  prepare(sql: string): {
    bind(...values: unknown[]): {
      first<T = Record<string, unknown>>(): Promise<T | null>;
    };
  };
};

type RepairR2Head = {
  key?: string;
  size: number;
  etag?: string;
  httpMetadata?: Record<string, unknown>;
  customMetadata?: Record<string, string>;
};

type RepairR2Body = RepairR2Head & {
  arrayBuffer(): Promise<ArrayBuffer>;
};

type RepairR2 = {
  head(key: string): Promise<RepairR2Head | null>;
  get(key: string): Promise<RepairR2Body | null>;
  put(key: string, value: Uint8Array, options?: unknown): Promise<unknown>;
};

export type R2ReleaseMetadataRepairRuntime = {
  DB?: RepairD1;
  RAW_BUCKET?: RepairR2;
  INGEST_API_KEY?: string;
  R2_METADATA_REPAIR_ENABLED?: string;
};

export type R2ReleaseMetadataRepairInput = {
  asOf: string;
  releaseId: string;
  artifactName: string;
  mode: "dry-run" | "apply";
};

export type R2ReleaseMetadataRepairResult = {
  contract: typeof R2_RELEASE_METADATA_REPAIR_CONTRACT;
  status:
    | "repaired"
    | "already_verified"
    | "verified_missing_metadata"
    | "metadata_completion_required";
  asOf: string;
  releaseId: string;
  artifactName: string;
  key: string;
  expectedSha256: string;
  expectedBytes: number;
  actualBytes: number;
  mode: "dry-run" | "apply";
  d1RowsRead: 1;
  d1RowsWritten: 0;
  r2ClassA: 0 | 1;
  r2ClassB: number;
  writePerformed: boolean;
};

function isCalendarDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return date.toISOString().slice(0, 10) === value;
}

function secureJsonParse(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("manifest_invalid");
  return parsed as Record<string, unknown>;
}

function descriptorFromManifest(
  row: D1Row,
  input: R2ReleaseMetadataRepairInput,
): { key: string; sha256: string; bytes: number } {
  const manifest = secureJsonParse(row.manifest_json);
  if (!isReadModelManifest(manifest as unknown)) throw new Error("manifest_invalid");
  const typedManifest = manifest as unknown as ReadModelManifest;
  if (typedManifest.asOf !== input.asOf || typedManifest.releaseId !== input.releaseId) {
    throw new Error("manifest_identity_mismatch");
  }
  const value = typedManifest.artifacts[input.artifactName];
  if (!value) throw new Error("artifact_not_declared");
  const key = typeof value.key === "string" ? value.key : "";
  const sha256 = typeof value.sha256 === "string" ? value.sha256 : "";
  const bytes = value.bytes;
  const prefix = `market-data/releases/${input.asOf}/${input.releaseId}/`;
  if (
    !key ||
    key.length > 512 ||
    !key.startsWith(prefix) ||
    key !== `${prefix}${input.artifactName}.json` ||
    key.includes("..") ||
    key.includes("//") ||
    /[\r\n]/.test(key)
  ) throw new Error("artifact_key_invalid");
  if (!SHA256.test(sha256)) throw new Error("artifact_sha256_invalid");
  if (!Number.isSafeInteger(bytes) || (bytes as number) <= 0 || (bytes as number) > MAX_OBJECT_BYTES) {
    throw new Error("artifact_size_invalid");
  }
  return { key, sha256, bytes: bytes as number };
}

export function parseR2ReleaseMetadataRepairInput(value: unknown): R2ReleaseMetadataRepairInput {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_repair_request");
  const candidate = value as Record<string, unknown>;
  const asOf = typeof candidate.asOf === "string" ? candidate.asOf : "";
  const releaseId = typeof candidate.releaseId === "string" ? candidate.releaseId : "";
  const artifactName = typeof candidate.artifactName === "string" ? candidate.artifactName : "";
  const mode = candidate.mode;
  if (!isCalendarDate(asOf)) throw new Error("invalid_as_of");
  if (!RELEASE_ID.test(releaseId) || !releaseId.startsWith(`release:${asOf}:`)) throw new Error("invalid_release_id");
  if (!READ_MODEL_NAME.test(artifactName)) throw new Error("invalid_artifact_name");
  if (mode !== "dry-run" && mode !== "apply") throw new Error("invalid_mode");
  return { asOf, releaseId, artifactName, mode };
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as unknown as BufferSource);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function responseBase(
  input: R2ReleaseMetadataRepairInput,
  descriptor: { key: string; sha256: string; bytes: number },
  status: R2ReleaseMetadataRepairResult["status"],
  actualBytes: number,
  r2ClassA: 0 | 1,
  r2ClassB: number,
  writePerformed: boolean,
): R2ReleaseMetadataRepairResult {
  return {
    contract: R2_RELEASE_METADATA_REPAIR_CONTRACT,
    status,
    asOf: input.asOf,
    releaseId: input.releaseId,
    artifactName: input.artifactName,
    key: descriptor.key,
    expectedSha256: descriptor.sha256,
    expectedBytes: descriptor.bytes,
    actualBytes,
    mode: input.mode,
    d1RowsRead: 1,
    d1RowsWritten: 0,
    r2ClassA,
    r2ClassB,
    writePerformed,
  };
}

/**
 * Verify and, only in apply mode, repair one exact release artifact. The
 * caller must enforce the route-level feature flag and existing admin auth.
 */
export async function repairR2ReleaseMetadata(
  runtime: R2ReleaseMetadataRepairRuntime,
  input: R2ReleaseMetadataRepairInput,
): Promise<R2ReleaseMetadataRepairResult> {
  if (!runtime.DB || !runtime.RAW_BUCKET) throw new Error("storage_unavailable");
  if (input.mode === "apply" && runtime.R2_METADATA_REPAIR_ENABLED !== "1") throw new Error("repair_disabled");

  const row = await runtime.DB
    .prepare(
      `SELECT as_of, release_id, status, manifest_json
         FROM release_manifest
        WHERE as_of = ? AND release_id = ?
        LIMIT 1`,
    )
    .bind(input.asOf, input.releaseId)
    .first<D1Row>();
  if (!row) throw new Error("release_not_found");
  if (!["ACTIVE", "SUPERSEDED", "STAGED"].includes(row.status)) throw new Error("release_status_invalid");
  const descriptor = descriptorFromManifest(row, input);
  const head = await runtime.RAW_BUCKET.head(descriptor.key);
  if (!head) throw new Error("r2_object_missing");
  const existingMetadata = head.customMetadata ?? {};
  if (existingMetadata.releaseId && existingMetadata.releaseId !== input.releaseId) throw new Error("r2_release_metadata_mismatch");
  if (existingMetadata.asOf && existingMetadata.asOf !== input.asOf) throw new Error("r2_as_of_metadata_mismatch");
  if (existingMetadata.sha256 && existingMetadata.sha256 !== descriptor.sha256) throw new Error("r2_hash_mismatch");
  if (head.size !== descriptor.bytes) throw new Error("r2_size_mismatch");

  const metadataComplete =
    existingMetadata.sha256 === descriptor.sha256 &&
    existingMetadata.releaseId === input.releaseId &&
    existingMetadata.asOf === input.asOf;
  if (metadataComplete) {
    return responseBase(input, descriptor, "already_verified", descriptor.bytes, 0, 1, false);
  }

  const object = await runtime.RAW_BUCKET.get(descriptor.key);
  if (!object) throw new Error("r2_object_missing");
  if (object.size !== descriptor.bytes || object.size > MAX_OBJECT_BYTES) throw new Error("r2_size_mismatch");
  const bytes = new Uint8Array(await object.arrayBuffer());
  if (bytes.byteLength !== descriptor.bytes) throw new Error("r2_size_mismatch");
  const actualSha256 = await sha256Hex(bytes);
  if (actualSha256 !== descriptor.sha256) throw new Error("r2_body_hash_mismatch");
  if (input.mode === "dry-run") {
    return responseBase(input, descriptor, "verified_missing_metadata", bytes.byteLength, 0, 2, false);
  }

  const customMetadata = {
    ...existingMetadata,
    releaseId: input.releaseId,
    asOf: input.asOf,
    sha256: descriptor.sha256,
  };
  const options: Record<string, unknown> = { customMetadata };
  if (object.httpMetadata) options.httpMetadata = object.httpMetadata;
  if (head.etag) options.onlyIf = { etagMatches: head.etag };
  await runtime.RAW_BUCKET.put(descriptor.key, bytes, options);
  const after = await runtime.RAW_BUCKET.head(descriptor.key);
  if (
    !after ||
    after.size !== descriptor.bytes ||
    after.customMetadata?.sha256 !== descriptor.sha256 ||
    after.customMetadata?.releaseId !== input.releaseId ||
    after.customMetadata?.asOf !== input.asOf
  ) throw new Error("r2_repair_verification_failed");
  return responseBase(input, descriptor, "repaired", bytes.byteLength, 1, 3, true);
}

export const R2_RELEASE_METADATA_REPAIR_MAX_INPUT_BYTES = MAX_INPUT_BYTES;
export const R2_RELEASE_METADATA_REPAIR_MAX_OBJECT_BYTES = MAX_OBJECT_BYTES;
