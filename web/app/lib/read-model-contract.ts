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
export const CAPABILITY_CONTRACT_VERSION = "flow-capability-v0.7.0";
export const CAPABILITY_MANIFEST_SCHEMA_VERSION = 4;
export const CAPABILITY_IDS = [
  "etf_daily_core",
  "etf_radar",
  "rotation",
  "national_team",
  "connect",
  "margin",
  "etf_periods",
  "etf_penetration",
  "stock_search",
  "stock_detail",
] as const;
export type ReadModelCapabilityId = (typeof CAPABILITY_IDS)[number];
export const ETF_PERIOD_IDS = ["1d", "3d", "1w", "2w", "1m", "3m", "6m", "ytd", "12m"] as const;
export type EtfPeriodId = (typeof ETF_PERIOD_IDS)[number];
export type CapabilityStatus =
  | "READY"
  | "PARTIAL"
  | "MISSING"
  | "WAITING_SOURCES"
  | "SOURCE_UNAVAILABLE"
  | "BLOCKED_QUALITY"
  | "UNKNOWN";
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

export type CapabilityArtifactReference = {
  name: string;
  key: string;
  sha256: string;
  bytes: number;
};

export type PeriodMetricCapability = {
  status: "READY" | "PARTIAL" | "MISSING" | "UNKNOWN";
  sampleCount: number;
  availableSampleCount: number;
  missingSampleCount: number;
  reasonCode: string | null;
};

export type ReadModelCapability = {
  status: CapabilityStatus;
  asOf: string | null;
  sourceDates: Record<string, string | null>;
  artifactRefs: CapabilityArtifactReference[];
  reasonCode: string | null;
  coverage: Record<string, number>;
  periods?: Record<string, Record<"shares" | "estimatedAmount", PeriodMetricCapability>>;
};

export type ReadModelCapabilityContract = {
  version: typeof CAPABILITY_CONTRACT_VERSION;
  scope: ReadModelSlot | "full";
  capabilities: Record<ReadModelCapabilityId, ReadModelCapability>;
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
  capabilityContract?: ReadModelCapabilityContract;
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

const CAPABILITY_OWNER: Record<ReadModelCapabilityId, ReadModelSlot> = {
  etf_daily_core: "dashboard",
  etf_radar: "dashboard",
  rotation: "dashboard",
  national_team: "dashboard",
  connect: "dashboard",
  margin: "dashboard",
  etf_periods: "dashboard",
  etf_penetration: "etf_penetration",
  stock_search: "etf_penetration",
  stock_detail: "etf_penetration",
};

const CAPABILITY_ARTIFACTS: Record<ReadModelCapabilityId, readonly string[]> = {
  etf_daily_core: ["bootstrap", "radar", "rotation"],
  etf_radar: ["radar"],
  rotation: ["rotation"],
  national_team: ["national"],
  connect: ["connect"],
  margin: ["margin"],
  etf_periods: ["radar", "rotation"],
  etf_penetration: ["radar_flow", "pressure"],
  stock_search: ["stock_search"],
  stock_detail: ["stock_0", "stock_1", "stock_2", "stock_3", "stock_4", "stock_5", "stock_6", "stock_7"],
};

const CAPABILITY_STATUSES = new Set<CapabilityStatus>([
  "READY",
  "PARTIAL",
  "MISSING",
  "WAITING_SOURCES",
  "SOURCE_UNAVAILABLE",
  "BLOCKED_QUALITY",
  "UNKNOWN",
]);

function isNonnegativeSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}

function isValidIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00.000Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}

function isCapabilityReference(
  value: unknown,
  name: string,
  manifest: ReadModelManifest,
): value is CapabilityArtifactReference {
  if (!isRecord(value)) return false;
  const descriptor = manifest.artifacts[name];
  return Boolean(
    isRecord(descriptor) &&
      value.name === name &&
      value.key === descriptor.key &&
      value.sha256 === descriptor.sha256 &&
      value.bytes === descriptor.bytes,
  );
}

function isPeriodMetricCapability(value: unknown): value is PeriodMetricCapability {
  if (!isRecord(value)) return false;
  const status = value.status;
  const sampleCount = value.sampleCount;
  const available = value.availableSampleCount;
  const missing = value.missingSampleCount;
  const reasonCode = value.reasonCode;
  if (
    (status !== "READY" && status !== "PARTIAL" && status !== "MISSING" && status !== "UNKNOWN") ||
    !isNonnegativeSafeInteger(sampleCount) ||
    !isNonnegativeSafeInteger(available) ||
    !isNonnegativeSafeInteger(missing) ||
    available + missing !== sampleCount ||
    (reasonCode !== null && (typeof reasonCode !== "string" || reasonCode.length === 0))
  ) {
    return false;
  }
  if (status === "READY") return sampleCount > 0 && available === sampleCount && missing === 0 && reasonCode === null;
  if (status === "PARTIAL") return sampleCount > 0 && available > 0 && available < sampleCount && reasonCode !== null;
  if (status === "MISSING") return sampleCount > 0 && available === 0 && reasonCode !== null;
  return reasonCode !== null;
}

function isPeriodCapability(value: ReadModelCapability): boolean {
  const periods = value.periods;
  if (!isRecord(periods) || Object.keys(periods).length !== ETF_PERIOD_IDS.length) return false;
  let readyCount = 0;
  let presentCount = 0;
  let unknownCount = 0;
  for (const period of ETF_PERIOD_IDS) {
    const metrics = periods[period];
    if (!isRecord(metrics) || Object.keys(metrics).length !== 2) return false;
    for (const metric of ["shares", "estimatedAmount"] as const) {
      const state = metrics[metric];
      if (!isPeriodMetricCapability(state)) return false;
      if (state.status === "READY") readyCount += 1;
      if (state.status === "READY" || state.status === "PARTIAL") presentCount += 1;
      if (state.status === "UNKNOWN") unknownCount += 1;
    }
  }
  const expected = ETF_PERIOD_IDS.length * 2;
  const declared = value.coverage;
  if (
    declared.periodCount !== ETF_PERIOD_IDS.length ||
    declared.readyPeriodMetricCount !== readyCount ||
    declared.expectedPeriodMetricCount !== expected
  ) {
    return false;
  }
  if (value.status === "READY") return readyCount === expected && value.reasonCode === null;
  if (value.status === "PARTIAL") return presentCount > 0 && readyCount < expected && unknownCount === 0 && value.reasonCode !== null;
  if (value.status === "MISSING") return presentCount === 0 && unknownCount === 0 && value.reasonCode !== null;
  if (value.status === "UNKNOWN") return unknownCount > 0 && value.reasonCode !== null;
  return false;
}

function isCapabilityContract(value: unknown, manifest: ReadModelManifest): value is ReadModelCapabilityContract {
  if (!isRecord(value)) return false;
  const expectedScope = manifest.slot ?? "full";
  if (value.version !== CAPABILITY_CONTRACT_VERSION || value.scope !== expectedScope || !isRecord(value.capabilities)) {
    return false;
  }
  const capabilityIds = Object.keys(value.capabilities);
  if (
    capabilityIds.length !== CAPABILITY_IDS.length ||
    CAPABILITY_IDS.some((capabilityId) => !Object.hasOwn(value.capabilities as object, capabilityId))
  ) {
    return false;
  }

  const capabilities = value.capabilities as Record<string, unknown>;
  for (const capabilityId of CAPABILITY_IDS) {
    const capability = capabilities[capabilityId];
    if (!isRecord(capability)) return false;
    const status = capability.status as CapabilityStatus;
    const asOf = capability.asOf;
    const sourceDates = capability.sourceDates;
    const reasonCode = capability.reasonCode;
    const artifactRefs = capability.artifactRefs;
    const coverage = capability.coverage;
    if (
      !CAPABILITY_STATUSES.has(status) ||
      (asOf !== null && (typeof asOf !== "string" || !ISO_DATE.test(asOf))) ||
      !isSourceDates(sourceDates) ||
      (reasonCode !== null && (typeof reasonCode !== "string" || reasonCode.length === 0)) ||
      !Array.isArray(artifactRefs) ||
      !isRecord(coverage) ||
      Object.values(coverage).some((count) => !isNonnegativeSafeInteger(count))
    ) {
      return false;
    }
    if ((status === "READY") !== (reasonCode === null)) return false;

    const isLocal = expectedScope === "full" || CAPABILITY_OWNER[capabilityId] === expectedScope;
    if (!isLocal) {
      if (
        status !== "UNKNOWN" ||
        asOf !== null ||
        reasonCode !== "separate_slot_not_bound" ||
        Object.keys(sourceDates).length !== 0 ||
        artifactRefs.length !== 0 ||
        Object.keys(coverage).length !== 0 ||
        capability.periods !== undefined
      ) {
        return false;
      }
      continue;
    }

    const expectedArtifacts = CAPABILITY_ARTIFACTS[capabilityId];
    if (
      artifactRefs.length !== expectedArtifacts.length ||
      expectedArtifacts.some((name, index) => !isCapabilityReference(artifactRefs[index], name, manifest))
    ) {
      return false;
    }
    if (status === "READY" && asOf !== manifest.asOf) return false;
    if (status !== "READY" && reasonCode === null) return false;

    if (capabilityId === "etf_daily_core" && status === "READY") {
      if (
        sourceDates.snapshot !== manifest.asOf ||
        sourceDates.sseEtfAsOf !== manifest.asOf ||
        sourceDates.szseEtfAsOf !== manifest.asOf ||
        !(capability.coverage.instrumentCount > 0) ||
        !(capability.coverage.tradingDays >= 2)
      ) return false;
    }
    if (capabilityId === "connect" && status === "READY" && (sourceDates.connectAsOf !== manifest.asOf || !(capability.coverage.recordCount > 0))) return false;
    if (capabilityId === "margin" && status === "READY" && (sourceDates.marginAsOf !== manifest.asOf || !(capability.coverage.recordCount > 0))) return false;
    if (
      capabilityId === "etf_penetration" &&
      status === "READY" &&
      (sourceDates.pcfEffectiveAsOf !== manifest.asOf || typeof sourceDates.pcfAsOf !== "string" ||
        !isValidIsoDate(sourceDates.pcfAsOf) || !(capability.coverage.stockCount > 0) || !(capability.coverage.matchedEtfCount > 0))
    ) return false;
    if (capabilityId === "etf_radar" && status === "READY" && !(capability.coverage.sampleCount > 0)) return false;
    if (capabilityId === "rotation" && status === "READY" && !(capability.coverage.sampleCount > 0)) return false;
    if (capabilityId === "national_team" && status === "READY" && !(capability.coverage.sampleCount > 0)) return false;
    if (
      ["stock_search", "stock_detail"].includes(capabilityId) &&
      status === "READY" &&
      (sourceDates.pcfEffectiveAsOf !== manifest.asOf || !(capability.coverage.stockCount > 0))
    ) return false;
    if (
      ["etf_radar", "rotation", "national_team", "etf_periods"].includes(capabilityId) &&
      status === "READY" &&
      capabilities.etf_daily_core &&
      (capabilities.etf_daily_core as Record<string, unknown>).status !== "READY"
    ) {
      return false;
    }
    if (
      ["etf_radar", "rotation", "national_team", "etf_periods"].includes(capabilityId) &&
      status === "READY" &&
      (sourceDates.sseEtfAsOf !== manifest.asOf || sourceDates.szseEtfAsOf !== manifest.asOf)
    ) return false;
    if (
      ["stock_search", "stock_detail"].includes(capabilityId) &&
      status === "READY" &&
      (capabilities.etf_penetration as Record<string, unknown> | undefined)?.status !== "READY"
    ) {
      return false;
    }
    if (["stock_search", "stock_detail"].includes(capabilityId) && status === "READY") {
      const penetration = capabilities.etf_penetration as Record<string, unknown>;
      const penetrationDates = penetration.sourceDates as Record<string, unknown>;
      if (
        sourceDates.pcfEffectiveAsOf !== manifest.asOf ||
        !isValidIsoDate(sourceDates.pcfAsOf) ||
        penetrationDates.pcfEffectiveAsOf !== sourceDates.pcfEffectiveAsOf ||
        penetrationDates.pcfAsOf !== sourceDates.pcfAsOf
      ) return false;
    }
    if (capabilityId === "etf_periods") {
      if (!isPeriodCapability(capability as unknown as ReadModelCapability)) return false;
    } else if (capability.periods !== undefined) {
      return false;
    }
  }
  return true;
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
 * Accept deployed v2/v3 contracts and the additive v4 capability contract.
 * v4 is a candidate-only contract and is never eligible for activation.
 */
export function isReadModelManifest(value: unknown): value is ReadModelManifest {
  if (!isRecord(value)) return false;
  if (
    (value.schemaVersion !== 2 && value.schemaVersion !== 3 && value.schemaVersion !== CAPABILITY_MANIFEST_SCHEMA_VERSION) ||
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

  if (value.schemaVersion >= 3) {
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
  } else if (value.capabilityContract !== undefined) {
    return false;
  }

  if (value.schemaVersion === CAPABILITY_MANIFEST_SCHEMA_VERSION) {
    if (!isValidIsoDate(value.asOf)) return false;
    const expectedPrefix = `market-data/releases/${value.asOf}/${value.releaseId}/`;
    if (names.some((name) => (artifacts[name] as ReadModelArtifactDescriptor).key !== `${expectedPrefix}${name}.json`)) return false;
  }
  if (value.schemaVersion === CAPABILITY_MANIFEST_SCHEMA_VERSION && !isCapabilityContract(value.capabilityContract, value as unknown as ReadModelManifest)) return false;
  if (value.schemaVersion !== CAPABILITY_MANIFEST_SCHEMA_VERSION && value.capabilityContract !== undefined) return false;

  return true;
}

export function hasReadyCapability(manifest: ReadModelManifest, capabilityId: ReadModelCapabilityId): boolean {
  return manifest.schemaVersion === CAPABILITY_MANIFEST_SCHEMA_VERSION &&
    manifest.capabilityContract?.capabilities[capabilityId]?.status === "READY";
}

export function isFullCapabilitySetReady(manifest: ReadModelManifest): boolean {
  return manifest.schemaVersion === CAPABILITY_MANIFEST_SCHEMA_VERSION &&
    manifest.capabilityContract?.scope === "full" &&
    CAPABILITY_IDS.every((id) => manifest.capabilityContract!.capabilities[id].status === "READY");
}

export function isCandidateOnlyReadModelManifest(manifest: ReadModelManifest): boolean {
  return manifest.schemaVersion === CAPABILITY_MANIFEST_SCHEMA_VERSION;
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
