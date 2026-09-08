/**
 * The read-model manifest is the small control-plane contract shared by the
 * import endpoints, history picker, and operational tooling.  Keep this
 * module free of Worker bindings so it can be used by every route.
 */

export const MAX_READ_MODEL_ARTIFACTS = 20;
export const MAX_READ_MODEL_BYTES = 5_000_000;
export const READ_MODEL_NAME = /^(bootstrap|overview|radar|radar_flow|rotation|national|connect|margin|pressure|stock_search|stock_[0-7])$/;
export const READ_MODEL_SLOTS = ["dashboard", "etf_penetration"] as const;
export type ReadModelSlot = (typeof READ_MODEL_SLOTS)[number];
export const RELEASE_ID = /^release:\d{4}-\d{2}-\d{2}:[a-f0-9]{16}$/;
export const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
export const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?(?:Z|[+-]\d{2}:\d{2})$/;
export const SHA256 = /^[a-f0-9]{64}$/;

/** The views required before a date is exposed as a complete release. */
export const COMPLETE_READ_MODEL_NAMES = [
  "bootstrap",
  "overview",
  "radar",
  "radar_flow",
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
  "radar_flow",
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
export const MARKET_READ_MODEL_NAMES = [
  "bootstrap",
  "overview",
  "radar",
  "rotation",
  "national",
  "connect",
  "margin",
] as const;
export const PENETRATION_READ_MODEL_NAMES = [
  "radar_flow",
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

export type MarketHistoryCoverage = {
  from: string | null;
  to: string | null;
  points: number;
};

export type MarketHistoryManifest = {
  schemaVersion: 1;
  scopeVersion?: "sse-szse-v1" | "sse-szse-bse-v1";
  sha256: string | null;
  archiveFrom: string | null;
  archiveTo: string | null;
  archiveDates: number;
  margin: MarketHistoryCoverage;
  connect: MarketHistoryCoverage;
};

export type ReadModelManifest = {
  schemaVersion: number;
  slot?: ReadModelSlot;
  releaseId: string;
  asOf: string;
  observedAt?: string | null;
  producerBuildSha?: string;
  sourceDates?: Record<string, string | null>;
  marketHistory?: MarketHistoryManifest;
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

function isMarketHistoryCoverage(value: unknown): value is MarketHistoryCoverage {
  return Boolean(
    isRecord(value) &&
      (value.from === null || (typeof value.from === "string" && ISO_DATE.test(value.from))) &&
      (value.to === null || (typeof value.to === "string" && ISO_DATE.test(value.to))) &&
      Number.isSafeInteger(value.points) &&
      (value.points as number) >= 0,
  );
}

function isMarketHistoryManifest(value: unknown): value is MarketHistoryManifest {
  return Boolean(
    isRecord(value) &&
      value.schemaVersion === 1 &&
      (value.scopeVersion === undefined || value.scopeVersion === "sse-szse-v1" || value.scopeVersion === "sse-szse-bse-v1") &&
      (value.sha256 === null || (typeof value.sha256 === "string" && SHA256.test(value.sha256))) &&
      (value.archiveFrom === null || (typeof value.archiveFrom === "string" && ISO_DATE.test(value.archiveFrom))) &&
      (value.archiveTo === null || (typeof value.archiveTo === "string" && ISO_DATE.test(value.archiveTo))) &&
      Number.isSafeInteger(value.archiveDates) &&
      (value.archiveDates as number) >= 0 &&
      isMarketHistoryCoverage(value.margin) &&
      isMarketHistoryCoverage(value.connect),
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
    (value.slot !== undefined && !READ_MODEL_SLOTS.includes(value.slot as ReadModelSlot)) ||
    typeof value.releaseId !== "string" ||
    !RELEASE_ID.test(value.releaseId) ||
    typeof value.asOf !== "string" ||
    !ISO_DATE.test(value.asOf) ||
    (value.observedAt !== undefined && value.observedAt !== null && !isIsoInstant(value.observedAt)) ||
    (value.producerBuildSha !== undefined &&
      (typeof value.producerBuildSha !== "string" || !/^[0-9a-f]{7,64}$/.test(value.producerBuildSha))) ||
    (value.sourceDates !== undefined && !isSourceDates(value.sourceDates)) ||
    (value.marketHistory !== undefined && !isMarketHistoryManifest(value.marketHistory)) ||
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
    const slot = value.slot as ReadModelSlot | undefined;
    const expectedNames = slot === "etf_penetration"
      ? PENETRATION_READ_MODEL_NAMES
      : slot === "dashboard"
        ? MARKET_READ_MODEL_NAMES
        : READ_MODEL_ORDER;
    if (
      !Array.isArray(value.requiredArtifacts) ||
      value.requiredArtifacts.length !== expectedNames.length ||
      new Set(value.requiredArtifacts).size !== names.length ||
      value.requiredArtifacts.some((name) => typeof name !== "string" || !names.includes(name)) ||
      JSON.stringify(value.requiredArtifacts) !==
        JSON.stringify(expectedNames)
    ) {
      return false;
    }
  }

  return true;
}

export function hasCompleteReadModelManifest(manifest: ReadModelManifest): boolean {
  const names = new Set(Object.keys(manifest.artifacts));
  const required = manifest.slot === "etf_penetration"
    ? PENETRATION_READ_MODEL_NAMES
    : manifest.slot === "dashboard"
      ? MARKET_READ_MODEL_NAMES
      : COMPLETE_READ_MODEL_NAMES;
  return required.every((name) => names.has(name));
}

/**
 * Historical inventory compatibility is intentionally less strict than the
 * current release activation contract.  Schema-v2 slotless releases were
 * published before ETF-to-stock penetration was introduced, so they are
 * complete when the seven original market artifacts are present.  Current
 * schema-v3 slots continue to use the strict activation requirements above.
 * This helper is for read-only recovery evidence only; it must not be used by
 * production release selection or date visibility.
 */
export function hasHistoricalCompatibleReadModelManifest(manifest: ReadModelManifest): boolean {
  if (manifest.schemaVersion === 2 && manifest.slot === undefined) {
    const names = new Set(Object.keys(manifest.artifacts));
    return MARKET_READ_MODEL_NAMES.every((name) => names.has(name));
  }
  return hasCompleteReadModelManifest(manifest);
}
