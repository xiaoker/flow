import { parsePipelineRunKey } from "./pipeline-window.ts";
import { PipelineWindowError } from "./pipeline-window-recovery.ts";

export const MAX_RECOVERY_PLAN_BYTES = 300_000;

const SHA256 = /^[0-9a-f]{64}$/;
const PLAN_KEY = /^market-data\/recovery\/plans\/[A-Za-z0-9._:-]{1,300}\.json$/;
const RECOVERY_ID = /^[A-Za-z0-9:_-]{1,120}$/;
const PLAN_SCHEMA_VERSION = 1;
const PLAN_CONTRACT_VERSION = "recovery-execution-v1";
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z$/;
const IDENTIFIER = /^[A-Za-z0-9:_./-]{1,220}$/;
const RECOVERY_STAGES = new Set([
  "core_collection",
  "dashboard",
  "etf_penetration",
  "stock_flow",
  "pcf_exact",
  "publish",
]);

type RecoveryPlanBinding = {
  recoveryId: string;
  runKey: string;
  planSha256: string;
  scopeSha256: string;
};

type RecoveryPlanHead = {
  size?: number;
  customMetadata?: Record<string, string>;
};

type RecoveryPlanObject = RecoveryPlanHead & {
  arrayBuffer(): Promise<ArrayBuffer>;
};

type RecoveryPlanBucket = {
  head(key: string): Promise<RecoveryPlanHead | null>;
  get(key: string): Promise<RecoveryPlanObject | null>;
  put(
    key: string,
    value: Uint8Array,
    options: {
      httpMetadata: { contentType: string };
      customMetadata: Record<string, string>;
    },
  ): Promise<unknown>;
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

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export async function sha256Hex(bytes: ArrayBuffer | Uint8Array): Promise<string> {
  let source: ArrayBuffer;
  if (bytes instanceof Uint8Array) {
    source = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(source).set(bytes);
  } else {
    source = bytes;
  }
  const digest = await crypto.subtle.digest("SHA-256", source);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function validDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function isolatedPath(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("..") ||
    value.includes("\\") ||
    value.startsWith("/")
  ) {
    return false;
  }
  return value === "recovery" || value === ".recovery" || value.startsWith("recovery/") || value.startsWith(".recovery/");
}

function validPlanScope(plan: Record<string, unknown>): boolean {
  if (plan.schemaVersion !== PLAN_SCHEMA_VERSION || plan.contractVersion !== PLAN_CONTRACT_VERSION) return false;
  if (
    typeof plan.campaignId !== "string" ||
    !IDENTIFIER.test(plan.campaignId) ||
    typeof plan.recoveryId !== "string" ||
    !IDENTIFIER.test(plan.recoveryId)
  ) {
    return false;
  }
  if (!parsePipelineRunKey(String(plan.runKey ?? ""))) return false;
  if (!Array.isArray(plan.dataDates) || plan.dataDates.length < 1 || plan.dataDates.length > 3) return false;
  const dates = plan.dataDates;
  if (
    dates.some((value) => !validDate(value)) ||
    new Set(dates).size !== dates.length ||
    dates.some((value, index) => index > 0 && String(dates[index - 1]) >= String(value))
  ) {
    return false;
  }
  if (
    !Array.isArray(plan.requiredStages) ||
    plan.requiredStages.length < 1 ||
    plan.requiredStages.some((value) => typeof value !== "string" || !RECOVERY_STAGES.has(value)) ||
    new Set(plan.requiredStages).size !== plan.requiredStages.length
  ) {
    return false;
  }
  if (!Number.isSafeInteger(plan.maxDates) || Number(plan.maxDates) < dates.length || Number(plan.maxDates) > 3) return false;
  if (!Number.isSafeInteger(plan.maxRequests) || Number(plan.maxRequests) < 1 || Number(plan.maxRequests) > 10_000) return false;
  if (typeof plan.deadline !== "string" || !ISO_INSTANT.test(plan.deadline) || Number.isNaN(Date.parse(plan.deadline))) return false;
  if (typeof plan.budgetManifest === "string") {
    if (!IDENTIFIER.test(plan.budgetManifest) || !(plan.budgetManifest.startsWith("ops-budget/") || plan.budgetManifest.startsWith("recovery/"))) {
      return false;
    }
  } else if (!isRecord(plan.budgetManifest)) {
    return false;
  }
  return isolatedPath(plan.artifactRoot) && isolatedPath(plan.stateDir);
}

function validateBinding(binding: RecoveryPlanBinding, status: number): void {
  if (!RECOVERY_ID.test(binding.recoveryId)) throw new PipelineWindowError("invalid_recovery_id", status);
  if (!parsePipelineRunKey(binding.runKey)) throw new PipelineWindowError("invalid_run_key", status);
  if (!SHA256.test(binding.planSha256)) throw new PipelineWindowError("invalid_plan_sha256", status);
  if (!SHA256.test(binding.scopeSha256)) throw new PipelineWindowError("invalid_scope_sha256", status);
}

export function recoveryPlanObjectKey(recoveryId: string, scopeSha256: string): string {
  if (!RECOVERY_ID.test(recoveryId)) throw new PipelineWindowError("invalid_recovery_id", 422);
  if (!SHA256.test(scopeSha256)) throw new PipelineWindowError("invalid_scope_sha256", 422);
  return `market-data/recovery/plans/${recoveryId}-${scopeSha256}.json`;
}

export async function validateRecoveryPlanBytes(
  bytes: Uint8Array,
  binding: RecoveryPlanBinding,
  status = 409,
): Promise<Record<string, unknown>> {
  validateBinding(binding, status);
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_RECOVERY_PLAN_BYTES) {
    throw new PipelineWindowError("plan_object_size_invalid", status);
  }
  if (!secureEqual(await sha256Hex(bytes), binding.scopeSha256)) {
    throw new PipelineWindowError("plan_object_sha256_mismatch", status);
  }
  let plan: unknown;
  try {
    plan = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new PipelineWindowError("plan_json_invalid", status);
  }
  if (
    !isRecord(plan) ||
    typeof plan.planHash !== "string" ||
    !SHA256.test(plan.planHash) ||
    !secureEqual(plan.planHash, binding.planSha256) ||
    plan.recoveryId !== binding.recoveryId ||
    plan.runKey !== binding.runKey
  ) {
    throw new PipelineWindowError("plan_binding_mismatch", status);
  }
  const withoutHash = { ...plan };
  delete withoutHash.planHash;
  const semanticHash = await sha256Hex(new TextEncoder().encode(canonicalJson(withoutHash)));
  if (!secureEqual(semanticHash, binding.planSha256)) {
    throw new PipelineWindowError("plan_semantic_sha256_mismatch", status);
  }
  if (!validPlanScope(plan)) throw new PipelineWindowError("plan_scope_invalid", status);
  return plan;
}

async function verifiedExistingPlan(
  bucket: RecoveryPlanBucket,
  key: string,
  binding: RecoveryPlanBinding,
): Promise<Record<string, unknown>> {
  const head = await bucket.head(key);
  if (
    !head ||
    head.size === undefined ||
    head.size <= 0 ||
    head.size > MAX_RECOVERY_PLAN_BYTES ||
    head.customMetadata?.sha256 !== binding.scopeSha256 ||
    (head.customMetadata?.recoveryId !== undefined && head.customMetadata.recoveryId !== binding.recoveryId) ||
    (head.customMetadata?.runKey !== undefined && head.customMetadata.runKey !== binding.runKey) ||
    (head.customMetadata?.planSha256 !== undefined && head.customMetadata.planSha256 !== binding.planSha256)
  ) {
    throw new PipelineWindowError("plan_object_not_verified", 409);
  }
  const object = await bucket.get(key);
  if (!object) throw new PipelineWindowError("plan_object_not_found", 409);
  return validateRecoveryPlanBytes(new Uint8Array(await object.arrayBuffer()), binding, 409);
}

export async function readRecoveryPlanObject(
  bucket: RecoveryPlanBucket,
  key: string,
  binding: RecoveryPlanBinding,
): Promise<Record<string, unknown>> {
  if (!PLAN_KEY.test(key) || key.includes("..") || key.includes("//")) {
    throw new PipelineWindowError("invalid_plan_key", 422);
  }
  return verifiedExistingPlan(bucket, key, binding);
}

export async function storeImmutableRecoveryPlan(
  bucket: RecoveryPlanBucket,
  bytes: Uint8Array,
  binding: RecoveryPlanBinding,
): Promise<{ decision: "stored" | "already_stored"; key: string; plan: Record<string, unknown> }> {
  const plan = await validateRecoveryPlanBytes(bytes, binding, 422);
  const key = recoveryPlanObjectKey(binding.recoveryId, binding.scopeSha256);
  const existing = await bucket.head(key);
  if (existing) {
    const verified = await verifiedExistingPlan(bucket, key, binding);
    return { decision: "already_stored", key, plan: verified };
  }

  await bucket.put(key, bytes, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: {
      sha256: binding.scopeSha256,
      recoveryId: binding.recoveryId,
      runKey: binding.runKey,
      planSha256: binding.planSha256,
    },
  });

  try {
    await verifiedExistingPlan(bucket, key, binding);
  } catch (error: unknown) {
    if (error instanceof PipelineWindowError && error.status === 409) {
      throw new PipelineWindowError("plan_object_write_verification_failed", 503);
    }
    throw error;
  }
  return { decision: "stored", key, plan };
}
