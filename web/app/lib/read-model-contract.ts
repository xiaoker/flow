/**
 * The read-model manifest is the small control-plane contract shared by the
 * import endpoints, history picker, and operational tooling.  Keep this
 * module free of Worker bindings so it can be used by every route.
 */

export const MAX_READ_MODEL_ARTIFACTS = 20;
export const MAX_READ_MODEL_BYTES = 5_000_000;
export const READ_MODEL_NAME = /^(bootstrap|overview|radar|rotation|national|connect|margin|pressure|stock_search|stock_[0-7])$/;
export const RELEASE_ID = /^release:\d{4}-\d{2}-\d{2}:[a-f0-9]{16}$/;
export const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
export const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?(?:Z|[+-]\d{2}:\d{2})$/;
export const SHA256 = /^[a-f0-9]{64}$/;

/** The views required before a date is exposed as a complete release. */
export const COMPLETE_READ_MODEL_NAMES = [
  "bootstrap",
  "overview",
  "radar",
  "rotation",
  "national",
  "connect",
  "margin",
  "pressure",
  "stock_search",
  "stock_0",
  "stock_1",
  "stock_2",
  "stock_3",
  "stock_4",
  "stock_5",
  "stock_6",
  "stock_7",
] as const;
export const READ_MODEL_ORDER = [
  "bootstrap",
  "overview",
  "radar",
  "rotation",
  "national",
  "connect",
  "margin",
  "pressure",
  "stock_search",
  "stock_0",
  "stock_1",
  "stock_2",
  "stock_3",
  "stock_4",
  "stock_5",
  "stock_6",
  "stock_7",
] as const;

export type ReadModelArtifactDescriptor = {
  key: string;
  sha256: string;
  bytes: number;
  contentType?: string;
};

export type ReadModelManifest = {
  schemaVersion: number;
  releaseId: string;
  asOf: string;
  observedAt?: string | null;
  sourceDates?: Record<string, string | null>;
  pressureSchemaVersion?: number;
  stockShardCount?: number;
  requiredArtifacts?: string[];
  manifestSha256?: string;
  artifacts: Record<string, ReadModelArtifactDescriptor>;
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isIsoInstant(value: unknown): value is string {
  return typeof value === "string" && ISO_INSTANT.test(value) && !Number.isNaN(Date.parse(value));
}

function isSourceDates(value: unknown): value is Record<string, string | null> {
  if (!isRecord(value)) return false;
  return Object.values(value).every(
    (date) => date === null || (typeof date === "string" && ISO_DATE.test(date)),
  );
}

function isArtifactDescriptor(value: unknown): value is ReadModelArtifactDescriptor {
  if (!isRecord(value)) return false;
  const key = value.key;
  const sha256 = value.sha256;
  const bytes = value.bytes;
  return Boolean(
    typeof key === "string" &&
      key.startsWith("market-data/releases/") &&
      !key.includes("..") &&
      typeof sha256 === "string" &&
      SHA256.test(sha256) &&
      Number.isSafeInteger(bytes) &&
      (bytes as number) > 0 &&
      (bytes as number) <= MAX_READ_MODEL_BYTES &&
      (value.contentType === undefined || typeof value.contentType === "string"),
  );
}

/**
 * Accept the deployed v2 producer and the additive v3 contract.  v3 fields
 * are required only when schemaVersion is 3, so an old collector can keep
 * publishing while the Worker migration is rolled out first.
 */
export function isReadModelManifest(value: unknown): value is ReadModelManifest {
  if (!isRecord(value)) return false;
  if (
    (value.schemaVersion !== 2 && value.schemaVersion !== 3) ||
    typeof value.releaseId !== "string" ||
    !RELEASE_ID.test(value.releaseId) ||
    typeof value.asOf !== "string" ||
    !ISO_DATE.test(value.asOf) ||
    (value.observedAt !== undefined && value.observedAt !== null && !isIsoInstant(value.observedAt)) ||
    (value.sourceDates !== undefined && !isSourceDates(value.sourceDates)) ||
    (value.manifestSha256 !== undefined &&
      (typeof value.manifestSha256 !== "string" || !SHA256.test(value.manifestSha256))) ||
    !isRecord(value.artifacts)
  ) {
    return false;
  }

  const artifacts = value.artifacts as Record<string, unknown>;
  const names = Object.keys(artifacts);
  if (
    !names.length ||
    names.length > MAX_READ_MODEL_ARTIFACTS ||
    names.some((name) => !READ_MODEL_NAME.test(name)) ||
    !names.every((name) => isArtifactDescriptor(artifacts[name]))
  ) {
    return false;
  }

  if (value.schemaVersion === 3) {
    if (!isIsoInstant(value.observedAt) || !(value.observedAt as string).endsWith("Z")) return false;
    if (!isSourceDates(value.sourceDates)) return false;
    const pressureSchemaVersion = value.pressureSchemaVersion;
    const stockShardCount = value.stockShardCount;
    if (pressureSchemaVersion !== 2) return false;
    if (stockShardCount !== 8) return false;
    if (
      !Array.isArray(value.requiredArtifacts) ||
      value.requiredArtifacts.length !== READ_MODEL_ORDER.length ||
      new Set(value.requiredArtifacts).size !== names.length ||
      value.requiredArtifacts.some((name) => typeof name !== "string" || !names.includes(name)) ||
      JSON.stringify(value.requiredArtifacts) !==
        JSON.stringify(READ_MODEL_ORDER)
    ) {
      return false;
    }
  }

  return true;
}

export function hasCompleteReadModelManifest(manifest: ReadModelManifest): boolean {
  const names = new Set(Object.keys(manifest.artifacts));
  return COMPLETE_READ_MODEL_NAMES.every((name) => names.has(name));
}
