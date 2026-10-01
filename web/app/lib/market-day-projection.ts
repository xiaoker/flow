import {
  canonicalJson,
  sha256Hex,
  type SourceObservationStatus,
} from "./source-observation.ts";
import type { MarketDayProbeMode } from "./market-day-mode.ts";

/**
 * v0.5 market-day projection.
 *
 * This is a compact, derived control-plane view.  It never replaces the
 * immutable source-observation receipts and it never decides that a release
 * is publishable.  The normal release builder remains the only component
 * allowed to construct and publish decision data.
 */
export const MARKET_DAY_PROJECTION_VERSION = "flow-market-day-v0.5.1" as const;
export const MARKET_DAY_STATE_SCHEMA_VERSION = 1 as const;
export const MARKET_DAY_STATE_LIMIT = 64;

export const MARKET_DAY_STATES = [
  "WAITING_SOURCES",
  "READY_TO_BUILD",
] as const;
export type MarketDayState = (typeof MARKET_DAY_STATES)[number];

export const MARKET_DAY_SOURCE_DEFINITIONS = [
  { key: "sse_etf", sourceId: "sse", dataset: "etf_scale", required: true },
  { key: "szse_etf", sourceId: "szse", dataset: "etf_scale", required: true },
  { key: "sse_margin", sourceId: "sse", dataset: "margin_market_daily", required: true },
  { key: "szse_margin", sourceId: "szse", dataset: "margin_market_daily", required: true },
  { key: "bse_margin", sourceId: "bse", dataset: "margin_market_daily", required: true },
  { key: "stock_connect_summary", sourceId: "hkex", dataset: "stock_connect_daily", required: true },
  { key: "sse_etf_nav", sourceId: "sse", dataset: "etf_nav", required: false },
  { key: "sse_etf_pcf", sourceId: "sse", dataset: "etf_pcf", required: false },
] as const;

export type MarketDaySourceKey = (typeof MARKET_DAY_SOURCE_DEFINITIONS)[number]["key"];

export type SourceObservationSummary = {
  observationId: string;
  sourceId: string;
  dataset: string;
  targetDate: string;
  sourceBusinessDate: string | null;
  publishedDate: string | null;
  status: SourceObservationStatus | string;
  sourceFingerprint: string;
  observedAt: string;
};

export type MarketDayProbeStatus =
  | "PUBLISHED"
  | "NOT_PUBLISHED"
  | "SOURCE_UNAVAILABLE"
  | "EMPTY_RESPONSE";

export type MarketDayProbe = {
  sourceKey: MarketDaySourceKey;
  sourceId: string;
  dataset: string;
  /** Scheduler business date used only to address the probe request. */
  windowDate?: string;
  /** Compatibility alias for older persisted probes; never a formal target date. */
  targetDate: string;
  status: MarketDayProbeStatus;
  httpStatus: number | null;
  observedAt: string;
  sourceBusinessDate: string | null;
  publishedDate: string | null;
  /** A bounded official-date hint. It is not a target_date binding or proof. */
  candidateBusinessDate?: string | null;
  fingerprint: string;
  reason: string;
  evidence: Record<string, unknown>;
};

export type MarketDayProjectionSource = {
  key: MarketDaySourceKey;
  sourceId: string;
  dataset: string;
  required: boolean;
  status: SourceObservationStatus | "UNKNOWN" | "DATE_MISMATCH";
  sourceBusinessDate: string | null;
  publishedDate: string | null;
  observationId: string | null;
  sourceFingerprint: string | null;
  observedAt: string | null;
  probe: MarketDayProbe | null;
  nextAction: "wait_for_publication" | "verify_observation" | "investigate_source" | "none";
};

export type MarketDayProjection = {
  schemaVersion: typeof MARKET_DAY_STATE_SCHEMA_VERSION;
  projectionVersion: typeof MARKET_DAY_PROJECTION_VERSION;
  /** Scheduler window; the persisted target_date key is this value. */
  windowDate: string;
  /** Compatibility alias retained for existing readers. */
  targetDate: string;
  /** Candidate from a probe only; the formal collector target remains unset. */
  candidateBusinessDate: string | null;
  formalTargetDate: string | null;
  state: MarketDayState;
  sourceFingerprint: string;
  probeFingerprint: string;
  counts: {
    required: number;
    verified: number;
    pending: number;
    unavailable: number;
    optionalVerified: number;
  };
  nextAction: "collect_sources" | "wait_for_source_change" | "build_release" | "investigate_source";
  sources: MarketDayProjectionSource[];
  probes: MarketDayProbe[];
  decision?: MarketDayDecision;
  projectedAt: string;
};

export type MarketDayDecision = {
  mode: MarketDayProbeMode;
  wouldDispatch: boolean;
  reason: string;
  candidateBusinessDate: string | null;
  formalTargetDate: null;
  oldDecision: "dispatch" | "skip" | "unknown";
  pipelineStatus: string | null;
};

export type MarketDayStateRow = {
  target_date: string;
  state: MarketDayState | string;
  projection_version: string;
  source_fingerprint: string;
  probe_fingerprint: string;
  required_source_count: number;
  verified_source_count: number;
  pending_source_count: number;
  unavailable_source_count: number;
  next_action: string;
  probe_json: string;
  sources_json: string;
  projected_at: string;
  created_at: string;
  updated_at: string;
};

type D1Result = { meta?: { changes?: unknown; rows_written?: unknown } };
type D1Statement = {
  bind(...values: unknown[]): D1Statement;
  run(): Promise<D1Result>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
};
export type MarketDayStateD1 = { prepare(sql: string): D1Statement };

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;

function isDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function isoInstant(value: unknown, fallback = new Date()): string {
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isNaN(parsed) ? fallback.toISOString() : new Date(parsed).toISOString();
}

function sourceDefinition(sourceId: string, dataset: string) {
  return MARKET_DAY_SOURCE_DEFINITIONS.find(
    (definition) => definition.sourceId === sourceId && definition.dataset === dataset,
  );
}

function sourceKey(definition: (typeof MARKET_DAY_SOURCE_DEFINITIONS)[number]): MarketDaySourceKey {
  return definition.key;
}

function latestObservations(observations: SourceObservationSummary[], targetDate: string): Map<MarketDaySourceKey, SourceObservationSummary> {
  const latest = new Map<MarketDaySourceKey, SourceObservationSummary>();
  for (const observation of observations) {
    if (!isDate(observation.targetDate) || observation.targetDate !== targetDate) continue;
    if (!SHA256.test(observation.sourceFingerprint)) continue;
    const definition = sourceDefinition(observation.sourceId, observation.dataset);
    if (!definition) continue;
    const key = sourceKey(definition);
    const previous = latest.get(key);
    if (!previous || Date.parse(observation.observedAt) > Date.parse(previous.observedAt) ||
      (observation.observedAt === previous.observedAt && observation.observationId > previous.observationId)) {
      latest.set(key, observation);
    }
  }
  return latest;
}

function statusForObservation(
  observation: SourceObservationSummary | undefined,
  targetDate: string,
): MarketDayProjectionSource["status"] {
  if (!observation) return "UNKNOWN";
  if (observation.status === "VERIFIED" && observation.sourceBusinessDate === targetDate) return "VERIFIED";
  if (observation.sourceBusinessDate && observation.sourceBusinessDate !== targetDate) return "DATE_MISMATCH";
  if (observation.status === "REJECTED_QUALITY" || observation.status === "SOURCE_UNAVAILABLE" || observation.status === "NOT_PUBLISHED") {
    return observation.status;
  }
  return observation.status || "UNKNOWN";
}

function actionForSource(
  status: MarketDayProjectionSource["status"],
  probe: MarketDayProbe | null,
): MarketDayProjectionSource["nextAction"] {
  if (status === "VERIFIED") return "none";
  if (status === "NOT_PUBLISHED" || probe?.status === "NOT_PUBLISHED" || probe?.status === "EMPTY_RESPONSE") {
    return "wait_for_publication";
  }
  if (status === "SOURCE_UNAVAILABLE" || probe?.status === "SOURCE_UNAVAILABLE") return "investigate_source";
  return "verify_observation";
}

async function aggregateFingerprint(value: unknown): Promise<string> {
  return sha256Hex(new TextEncoder().encode(canonicalJson(value)));
}

/**
 * Project the latest immutable observations plus lightweight probe results.
 * Missing observations remain UNKNOWN; they are never converted to zero or a
 * previous value.  The projection is deterministic apart from `projectedAt`.
 */
export async function projectMarketDay(input: {
  windowDate?: string;
  /** Deprecated compatibility input; interpreted as the scheduler window only. */
  targetDate?: string;
  observations?: SourceObservationSummary[];
  probes?: MarketDayProbe[];
  projectedAt?: string;
}): Promise<MarketDayProjection> {
  const windowDate = input.windowDate ?? input.targetDate ?? "";
  if (!isDate(windowDate)) throw new Error("market_day_invalid_window_date");
  if (input.windowDate && input.targetDate && input.windowDate !== input.targetDate) {
    throw new Error("market_day_window_target_conflict");
  }
  const probes = [...(input.probes ?? [])]
    .filter((probe) => (probe.windowDate ?? probe.targetDate) === windowDate)
    .sort((left, right) => left.sourceKey.localeCompare(right.sourceKey));
  const probeByKey = new Map(probes.map((probe) => [probe.sourceKey, probe]));
  const latest = latestObservations(input.observations ?? [], windowDate);
  const sources: MarketDayProjectionSource[] = MARKET_DAY_SOURCE_DEFINITIONS.map((definition) => {
    const key = sourceKey(definition);
    const observation = latest.get(key);
    const probe = probeByKey.get(key) ?? null;
    const status = statusForObservation(observation, windowDate);
    return {
      key,
      sourceId: definition.sourceId,
      dataset: definition.dataset,
      required: definition.required,
      status,
      sourceBusinessDate: observation?.sourceBusinessDate ?? probe?.sourceBusinessDate ?? null,
      publishedDate: observation?.publishedDate ?? probe?.publishedDate ?? null,
      observationId: observation?.observationId ?? null,
      sourceFingerprint: observation?.sourceFingerprint ?? null,
      observedAt: observation?.observedAt ?? probe?.observedAt ?? null,
      probe,
      nextAction: actionForSource(status, probe),
    };
  });
  const required = sources.filter((source) => source.required);
  const verified = required.filter((source) => source.status === "VERIFIED").length;
  const pending = required.filter((source) => !["VERIFIED", "SOURCE_UNAVAILABLE", "REJECTED_QUALITY", "DATE_MISMATCH"].includes(source.status)).length;
  const unavailable = required.filter((source) => ["SOURCE_UNAVAILABLE", "REJECTED_QUALITY", "DATE_MISMATCH"].includes(source.status)).length;
  const optionalVerified = sources.filter((source) => !source.required && source.status === "VERIFIED").length;
  const state: MarketDayState = verified === required.length ? "READY_TO_BUILD" : "WAITING_SOURCES";
  const candidateDates = [...new Set(
    probes
      .map((probe) => probe.candidateBusinessDate ?? probe.sourceBusinessDate ?? null)
      .filter((date): date is string => Boolean(date) && isDate(date)),
  )];
  const candidateBusinessDate = candidateDates.length === 1 ? candidateDates[0] : null;
  const sourceFingerprint = await aggregateFingerprint(
    sources.map((source) => ({
      key: source.key,
      status: source.status,
      sourceBusinessDate: source.sourceBusinessDate,
      sourceFingerprint: source.sourceFingerprint,
      observationId: source.observationId,
    })),
  );
  const probeFingerprint = await aggregateFingerprint(
    probes.map((probe) => ({
      sourceKey: probe.sourceKey,
      status: probe.status,
      httpStatus: probe.httpStatus,
      sourceBusinessDate: probe.sourceBusinessDate,
      publishedDate: probe.publishedDate,
      candidateBusinessDate: probe.candidateBusinessDate ?? null,
      fingerprint: probe.fingerprint,
    })),
  );
  const nextAction = state === "READY_TO_BUILD"
    ? "build_release"
    : unavailable > 0
      ? "investigate_source"
      : pending > 0
        ? "collect_sources"
        : "wait_for_source_change";
  return {
    schemaVersion: MARKET_DAY_STATE_SCHEMA_VERSION,
    projectionVersion: MARKET_DAY_PROJECTION_VERSION,
    windowDate,
    targetDate: windowDate,
    candidateBusinessDate,
    formalTargetDate: null,
    state,
    sourceFingerprint,
    probeFingerprint,
    counts: {
      required: required.length,
      verified,
      pending,
      unavailable,
      optionalVerified,
    },
    nextAction,
    sources,
    probes,
    projectedAt: isoInstant(input.projectedAt),
  };
}

/**
 * Decide whether a heavy collector may be dispatched after a probe.  A first
 * probe or changed fingerprint is enough only when at least one source is
 * plausibly available.  A retryable pipeline failure is also eligible so a
 * crash after state persistence cannot strand a window forever.  Stable
 * not-published/unavailable probes do not keep spending collection budget.
 */
export function shouldDispatchHeavyCollection(
  previous: Pick<MarketDayProjection, "probeFingerprint" | "state"> | null,
  current: Pick<MarketDayProjection, "probeFingerprint" | "state" | "probes"> & Partial<Pick<MarketDayProjection, "candidateBusinessDate">>,
  pipelineStatus: string | null,
): boolean {
  return decideHeavyCollection(previous, {
    ...current,
    candidateBusinessDate: current.candidateBusinessDate ?? null,
  }, pipelineStatus).wouldDispatch;
}

/**
 * Produce the explicit decision recorded by OBSERVE and consumed by ENFORCE.
 * A probe is a hint only: verified source observations and the publisher still
 * own the formal target-date proof.  In particular, a stable 200 response or
 * a publication watermark cannot by itself dispatch a heavy collection.
 */
export function decideHeavyCollection(
  previous: Pick<MarketDayProjection, "probeFingerprint" | "state"> | null,
  current: Pick<MarketDayProjection, "probeFingerprint" | "state" | "probes" | "candidateBusinessDate">,
  pipelineStatus: string | null,
  mode: MarketDayProbeMode = "ENFORCE",
  oldDecision: MarketDayDecision["oldDecision"] = "dispatch",
): MarketDayDecision {
  const sourceMayBeAvailable = current.probes.some((probe) => probe.status === "PUBLISHED");
  let wouldDispatch = false;
  let reason = "stable_not_published_or_unavailable";

  if (pipelineStatus === "FAILED_RETRYABLE") {
    wouldDispatch = true;
    reason = "retryable_pipeline_failure";
  } else if (current.state === "READY_TO_BUILD") {
    // READY_TO_BUILD is reusable input, not a terminal skip.  A release that
    // has not completed still needs a chance to build/publish from the same
    // immutable observations after a crash.
    wouldDispatch = pipelineStatus !== "COMPLETED" && pipelineStatus !== "DISPATCHED" && pipelineStatus !== "RUNNING";
    reason = wouldDispatch ? "ready_to_build_without_completed_release" : "ready_to_build_release_in_flight_or_complete";
  } else if (!sourceMayBeAvailable) {
    wouldDispatch = false;
    reason = current.probes.some((probe) => probe.status === "EMPTY_RESPONSE")
      ? "empty_response_not_evidence"
      : "no_published_probe";
  } else if (!previous) {
    wouldDispatch = true;
    reason = "first_probe_with_published_source";
  } else if (previous.probeFingerprint !== current.probeFingerprint) {
    wouldDispatch = true;
    reason = "probe_fingerprint_changed";
  } else {
    wouldDispatch = false;
    reason = "stable_probe_fingerprint";
  }

  return {
    mode,
    wouldDispatch,
    reason,
    candidateBusinessDate: current.candidateBusinessDate,
    formalTargetDate: null,
    oldDecision,
    pipelineStatus,
  };
}

function boundedJson(value: unknown, maxBytes: number): string {
  const encoded = new TextEncoder().encode(canonicalJson(value));
  if (encoded.byteLength > maxBytes) throw new Error("market_day_projection_payload_too_large");
  return new TextDecoder().decode(encoded);
}

/** Persist one bounded projection row; this is not a lease or a release pointer. */
export async function persistMarketDayProjection(
  db: MarketDayStateD1,
  projection: MarketDayProjection,
  now = new Date(),
): Promise<{ changes: number; rowsWritten: number }> {
  const nowIso = now.toISOString();
  const result = await db.prepare(`
    INSERT INTO market_day_state (
      target_date, state, projection_version, source_fingerprint, probe_fingerprint,
      required_source_count, verified_source_count, pending_source_count,
      unavailable_source_count, next_action, probe_json, sources_json,
      projected_at, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(target_date) DO UPDATE SET
      state = excluded.state,
      projection_version = excluded.projection_version,
      source_fingerprint = excluded.source_fingerprint,
      probe_fingerprint = excluded.probe_fingerprint,
      required_source_count = excluded.required_source_count,
      verified_source_count = excluded.verified_source_count,
      pending_source_count = excluded.pending_source_count,
      unavailable_source_count = excluded.unavailable_source_count,
      next_action = excluded.next_action,
      probe_json = excluded.probe_json,
      sources_json = excluded.sources_json,
      projected_at = excluded.projected_at,
      updated_at = excluded.updated_at
  `).bind(
    projection.targetDate,
    projection.state,
    projection.projectionVersion,
    projection.sourceFingerprint,
    projection.probeFingerprint,
    projection.counts.required,
    projection.counts.verified,
    projection.counts.pending,
    projection.counts.unavailable,
    projection.nextAction,
    boundedJson({
      probes: projection.probes,
      decision: projection.decision ?? null,
      candidateBusinessDate: projection.candidateBusinessDate,
      formalTargetDate: projection.formalTargetDate,
    }, 48 * 1024),
    boundedJson(projection.sources, 48 * 1024),
    projection.projectedAt,
    nowIso,
    nowIso,
  ).run();
  const changes = Number(result.meta?.changes);
  const rowsWritten = Number(result.meta?.rows_written);
  if (!Number.isSafeInteger(changes) || changes < 0 || changes > 1) {
    throw new Error("market_day_projection_change_meta_invalid");
  }
  if (!Number.isSafeInteger(rowsWritten) || rowsWritten < 0 || rowsWritten > 4) {
    throw new Error("market_day_projection_write_meta_invalid");
  }
  return { changes, rowsWritten };
}

export async function getMarketDayState(
  db: MarketDayStateD1,
  targetDate: string,
): Promise<MarketDayStateRow | null> {
  if (!isDate(targetDate)) throw new Error("market_day_invalid_target_date");
  return db.prepare(`
    SELECT target_date, state, projection_version, source_fingerprint, probe_fingerprint,
           required_source_count, verified_source_count, pending_source_count,
           unavailable_source_count, next_action, probe_json, sources_json,
           projected_at, created_at, updated_at
      FROM market_day_state
     WHERE target_date = ?
     LIMIT 1
  `).bind(targetDate).first<MarketDayStateRow>();
}

export function parseMarketDayStateRow(row: MarketDayStateRow): MarketDayProjection {
  if (!row || !isDate(row.target_date)) throw new Error("market_day_state_invalid_row");
  if (!MARKET_DAY_STATES.includes(row.state as MarketDayState)) throw new Error("market_day_state_invalid_state");
  const probePayload = JSON.parse(row.probe_json || "[]") as MarketDayProbe[] | {
    probes?: MarketDayProbe[];
    decision?: MarketDayDecision | null;
    candidateBusinessDate?: string | null;
    formalTargetDate?: string | null;
  };
  const probes = Array.isArray(probePayload) ? probePayload : probePayload.probes ?? [];
  const decision = Array.isArray(probePayload) ? undefined : probePayload.decision ?? undefined;
  const sources = JSON.parse(row.sources_json || "[]") as MarketDayProjectionSource[];
  if (!Array.isArray(probes) || !Array.isArray(sources)) throw new Error("market_day_state_invalid_json");
  return {
    schemaVersion: MARKET_DAY_STATE_SCHEMA_VERSION,
    projectionVersion: MARKET_DAY_PROJECTION_VERSION,
    windowDate: row.target_date,
    targetDate: row.target_date,
    candidateBusinessDate: Array.isArray(probePayload) ? null : probePayload.candidateBusinessDate ?? null,
    formalTargetDate: Array.isArray(probePayload) ? null : probePayload.formalTargetDate ?? null,
    state: row.state as MarketDayState,
    sourceFingerprint: row.source_fingerprint,
    probeFingerprint: row.probe_fingerprint,
    counts: {
      required: Number(row.required_source_count),
      verified: Number(row.verified_source_count),
      pending: Number(row.pending_source_count),
      unavailable: Number(row.unavailable_source_count),
      optionalVerified: sources.filter((source) => !source.required && source.status === "VERIFIED").length,
    },
    nextAction: row.next_action as MarketDayProjection["nextAction"],
    sources,
    probes,
    decision,
    projectedAt: row.projected_at,
  };
}
