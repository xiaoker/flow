import type {
  DashboardSnapshot,
  EtfTerminalData,
  EtfTerminalInstrument,
  FlowPeriod,
} from "@/app/lib/dashboard-data";

export const ETF_FLOW_STATE_VERSION = 1;
export const ETF_CLASSIFICATION_VERSION = "2026-07-v3";
export const ETF_FLOW_STATE_MAX_DATES = 260;
export const ETF_FLOW_STATE_POINTER_KEY =
  `market-data/derived/etf-flow-state/${ETF_CLASSIFICATION_VERSION}/latest.json`;
const UNCLASSIFIED_CATEGORY = "未归类";
const UNCLASSIFIED_GROUP_TYPE = "unclassified";

const PERIOD_WINDOWS: Partial<Record<FlowPeriod, number>> = {
  "1d": 1,
  "3d": 3,
  "1w": 5,
  "2w": 10,
  "1m": 21,
  "3m": 63,
  "6m": 126,
  "12m": 252,
};
const PERIOD_ORDER: FlowPeriod[] = [
  "1d",
  "3d",
  "1w",
  "2w",
  "1m",
  "3m",
  "6m",
  "ytd",
  "12m",
];

type StateInstrument = {
  code: string;
  name: string;
  market: "SSE" | "SZSE";
  category: string;
  groupType?: string;
  assetClass?: string;
  classificationSource?: string;
  classificationConfidence?: string;
  classificationRule?: string;
  includeInGroup?: boolean;
  values: Array<number | null>;
  /** Daily cash estimates in CNY 亿元, aligned with `dates`. Optional for v1 legacy state. */
  cashValues?: Array<number | null>;
};

export type EtfFlowState = {
  version: 1;
  classificationVersion: string;
  calculationMethod: "sum_of_daily_official_share_changes";
  cashCalculationMethod?: "sum_of_daily_share_changes_times_same_day_nav";
  generatedAt: string;
  dates: string[];
  instruments: StateInstrument[];
  sourceSnapshots: Array<{ date: string; sha256: string; file?: string }>;
  quality: {
    snapshotCount: number;
    instrumentCount: number;
    populatedValues: number;
    rejectedRows: number;
    duplicateRows: number;
    status: "pass" | "conditional_pass";
  };
};

/**
 * Server-side guard for the derived terminal contract.  Legacy snapshots may
 * omit periodCoverage and are left readable for migration; every newly built
 * terminal carries the field and must pass this check before publication.
 */
export function validateEtfTerminalCoverage(
  terminal: EtfTerminalData | undefined,
): string[] {
  if (!terminal || !terminal.instruments.length) return [];
  const contractPresent = terminal.instruments.some(
    (item) => item.periodCoverage !== undefined,
  );
  if (!contractPresent) return [];
  const periods = terminal.availablePeriods ?? PERIOD_ORDER;
  const issues: string[] = [];
  const keys = new Set<string>();
  for (const item of terminal.instruments) {
    const key = `${item.market ?? "SSE"}:${item.code}`;
    if (keys.has(key)) issues.push(`duplicate:${key}`);
    keys.add(key);
    for (const period of periods) {
      const expected = PERIOD_WINDOWS[period];
      const coverage = item.periodCoverage?.[period];
      const value = item.periods[period];
      if (expected === undefined) {
        if (value !== null && value !== undefined && !(coverage && coverage > 0)) {
          issues.push(`missing_coverage:${key}:${period}`);
        }
        continue;
      }
      if (!Number.isInteger(coverage) || (coverage ?? 0) < 0 || (coverage ?? 0) > expected) {
        issues.push(`invalid_coverage:${key}:${period}:${coverage ?? "null"}/${expected}`);
      } else if (value !== null && value !== undefined && coverage !== expected) {
        issues.push(`partial_value:${key}:${period}:${coverage}/${expected}`);
      } else if (value === null && coverage === expected) {
        issues.push(`missing_value:${key}:${period}`);
      }
      const cash = item.estimatedCashPeriods?.[period];
      if (cash !== null && cash !== undefined && item.cashPeriodCoverage?.[period] !== expected) {
        issues.push(`partial_cash:${key}:${period}:${item.cashPeriodCoverage?.[period] ?? "null"}/${expected}`);
      }
    }
  }
  for (const group of terminal.groupPeriods) {
    for (const [period, value] of Object.entries(group.periods)) {
      if (value !== null && value !== undefined && !(group.comparableCount?.[period as FlowPeriod] ?? 0)) {
        issues.push(`group_without_components:${group.name}:${period}`);
      }
    }
  }
  if (terminal.groupPeriods.length && terminal.scope === "SSE+SZSE") {
    for (const period of periods) {
      const instrumentTotal = terminal.instruments.reduce((sum, item) => {
        const value = item.periods[period];
        return typeof value === "number" ? sum + value : sum;
      }, 0);
      const groupTotal = terminal.groupPeriods.reduce((sum, group) => {
        const value = group.periods[period];
        return typeof value === "number" ? sum + value : sum;
      }, 0);
      const comparable = terminal.instruments.filter(
        (item) => typeof item.periods[period] === "number",
      ).length;
      // Values retain six decimal 亿份 precision at the terminal boundary, so a
      // small tolerance is expected. A larger difference means a product was
      // silently dropped or counted twice by the sector aggregation.
      if (comparable > 0 && Math.abs(groupTotal - instrumentTotal) > 0.01) {
        issues.push(
          `group_reconciliation:${period}:${groupTotal.toFixed(2)}:${instrumentTotal.toFixed(2)}`,
        );
      }
    }
  }
  return issues;
}

type StatePointer = {
  version: 1;
  classificationVersion: string;
  objectKey: string;
  sha256: string;
  asOf: string;
  dateCount: number;
  updatedAt: string;
};

export type OfficialEtfDailyChange = {
  code: string;
  market: "SSE" | "SZSE";
  value: number | null;
  /** The raw previous ETF snapshot used by the official delta query. */
  previousTradeDate?: string | null;
  /** The immediately preceding SSE ETF trading date used as the calendar anchor. */
  expectedPreviousTradeDate?: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isEtfFlowState(
  value: unknown,
  options: { deep?: boolean } = {},
): value is EtfFlowState {
  const deep = options.deep ?? true;
  if (!isRecord(value)) return false;
  if (
    value.version !== ETF_FLOW_STATE_VERSION ||
    value.classificationVersion !== ETF_CLASSIFICATION_VERSION ||
    value.calculationMethod !== "sum_of_daily_official_share_changes" ||
    !Array.isArray(value.dates) ||
    value.dates.length === 0 ||
    value.dates.length > ETF_FLOW_STATE_MAX_DATES ||
    !value.dates.every((date) => typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date)) ||
    !Array.isArray(value.instruments)
  ) {
    return false;
  }
  return value.instruments.every((item) => {
    if (!isRecord(item)) return false;
    const validShape = Boolean(
      typeof item.code === "string" &&
        /^\d{6}$/.test(item.code) &&
        (item.market === "SSE" || item.market === "SZSE") &&
        typeof item.name === "string" &&
        typeof item.category === "string" &&
        Array.isArray(item.values) &&
        item.values.length === value.dates.length,
    );
    if (!validShape) return false;
    if (!deep) return true;
    const validValues = item.values.every(
      (point) => point === null || (typeof point === "number" && Number.isFinite(point)),
    );
    const validCashValues =
      item.cashValues === undefined ||
      (Array.isArray(item.cashValues) &&
        item.cashValues.length === value.dates.length &&
        item.cashValues.every(
          (point) => point === null || (typeof point === "number" && Number.isFinite(point)),
        ));
    return validValues && validCashValues;
  });
}

async function sha256Hex(content: string): Promise<string> {
  const bytes = new TextEncoder().encode(content);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function loadEtfFlowState(
  bucket?: R2Bucket,
): Promise<EtfFlowState | null> {
  if (!bucket || typeof bucket.get !== "function") return null;
  const existingInFlight = etfFlowStateInFlight;
  if (existingInFlight?.bucket === bucket) return existingInFlight.promise;
  const promise = readEtfFlowState(bucket);
  etfFlowStateInFlight = { bucket, promise };
  try {
    return await promise;
  } finally {
    if (etfFlowStateInFlight?.promise === promise) etfFlowStateInFlight = null;
  }
}

type EtfFlowStateCache = {
  bucket: R2Bucket;
  objectKey: string;
  sha256: string;
  state: EtfFlowState | null;
};

let etfFlowStateCache: EtfFlowStateCache | null = null;
let etfFlowStateInFlight: { bucket: R2Bucket; promise: Promise<EtfFlowState | null> } | null = null;

async function readEtfFlowState(bucket: R2Bucket): Promise<EtfFlowState | null> {
  try {
    const pointerObject = await bucket.get(ETF_FLOW_STATE_POINTER_KEY);
    if (!pointerObject) return null;
    const pointer: unknown = JSON.parse(await pointerObject.text());
    if (
      !isRecord(pointer) ||
      pointer.version !== ETF_FLOW_STATE_VERSION ||
      pointer.classificationVersion !== ETF_CLASSIFICATION_VERSION ||
      typeof pointer.objectKey !== "string" ||
      typeof pointer.sha256 !== "string"
    ) {
      return null;
    }
    const objectKey = pointer.objectKey;
    const sha256 = pointer.sha256;
    if (
      etfFlowStateCache?.bucket === bucket &&
      etfFlowStateCache.objectKey === objectKey &&
      etfFlowStateCache.sha256 === sha256
    ) {
      return etfFlowStateCache.state;
    }
    const stateObject = await bucket.get(objectKey);
    if (!stateObject) return null;
    const metadataSha256 = stateObject.customMetadata?.sha256;
    const body = await stateObject.text();
    // Persisted objects carry the digest in metadata. This avoids hashing the
    // full (often ~1 MB) state body on every cold request. Legacy fixtures and
    // objects without metadata still use the full hash fallback.
    if (metadataSha256 !== sha256 && (await sha256Hex(body)) !== sha256) {
      etfFlowStateCache = { bucket, objectKey, sha256, state: null };
      return null;
    }
    const state: unknown = JSON.parse(body);
    const parsed = isEtfFlowState(state, { deep: false }) ? state : null;
    etfFlowStateCache = { bucket, objectKey, sha256, state: parsed };
    return parsed;
  } catch {
    return null;
  }
}

export async function persistEtfFlowState(
  bucket: R2Bucket,
  state: EtfFlowState,
): Promise<StatePointer> {
  if (!isEtfFlowState(state)) throw new Error("invalid_etf_flow_state");
  const body = JSON.stringify(state);
  const sha256 = await sha256Hex(body);
  const asOf = state.dates.at(-1) ?? "";
  const objectKey =
    `market-data/derived/etf-flow-state/${ETF_CLASSIFICATION_VERSION}/${asOf}/${sha256}.json`;
  await bucket.put(objectKey, body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: {
      classificationVersion: ETF_CLASSIFICATION_VERSION,
      calculationMethod: state.calculationMethod,
      sha256,
      asOf,
    },
  });
  const pointer: StatePointer = {
    version: ETF_FLOW_STATE_VERSION,
    classificationVersion: ETF_CLASSIFICATION_VERSION,
    objectKey,
    sha256,
    asOf,
    dateCount: state.dates.length,
    updatedAt: new Date().toISOString(),
  };
  await bucket.put(ETF_FLOW_STATE_POINTER_KEY, JSON.stringify(pointer), {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
    customMetadata: { sha256, asOf },
  });
  return pointer;
}

function instrumentKey(item: { market?: string; code: string }): string {
  return `${item.market ?? "SSE"}:${item.code}`;
}

export function mergeSnapshotIntoEtfFlowState(
  previous: EtfFlowState | null,
  snapshot: DashboardSnapshot,
  sourceSnapshotSha256: string,
  officialDailyChanges?: OfficialEtfDailyChange[],
): EtfFlowState | null {
  const terminal = snapshot.etfTerminal;
  if (
    !terminal ||
    terminal.classificationVersion !== ETF_CLASSIFICATION_VERSION ||
    !terminal.instruments.length
  ) {
    return previous;
  }
  // Keep the immediately preceding official date in the state calendar even
  // when no complete dashboard snapshot was published for that date.  The
  // placeholder carries a null value, so a 3d/1w sum cannot silently bridge a
  // source gap while the current 1d value can still remain valid.
  const expectedPreviousDates = (officialDailyChanges ?? [])
    .map((item) => item.expectedPreviousTradeDate)
    .filter(
      (date): date is string =>
        typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date) && date < snapshot.asOf,
    );
  const dates = [...new Set([...(previous?.dates ?? []), ...expectedPreviousDates, snapshot.asOf])]
    .sort()
    .slice(-ETF_FLOW_STATE_MAX_DATES);
  const dateIndex = new Map(dates.map((date, index) => [date, index]));
  const rows = new Map<string, StateInstrument>();

  for (const item of previous?.instruments ?? []) {
    const values = Array<number | null>(dates.length).fill(null);
    const cashValues = Array<number | null>(dates.length).fill(null);
    previous?.dates.forEach((date, index) => {
      const nextIndex = dateIndex.get(date);
      if (nextIndex !== undefined) {
        values[nextIndex] = item.values[index] ?? null;
        if (item.cashValues) cashValues[nextIndex] = item.cashValues[index] ?? null;
      }
    });
    rows.set(instrumentKey(item), { ...item, values, cashValues });
  }

  const currentIndex = dateIndex.get(snapshot.asOf);
  if (currentIndex === undefined) return previous;
  const hasOfficialChanges = officialDailyChanges !== undefined;
  const officialChanges = new Map(
    (officialDailyChanges ?? []).map((item) => [instrumentKey(item), item.value]),
  );
  for (const [key, value] of officialChanges) {
    if (value === null) {
      // Explicit verified nulls invalidate this date, including removed codes.
      // Missing map entries remain unknown and are never replaced with zero.
      const existing = rows.get(key);
      if (existing) {
        existing.values[currentIndex] = null;
        if (existing.cashValues) existing.cashValues[currentIndex] = null;
      }
    }
  }
  for (const item of terminal.instruments) {
    const key = instrumentKey(item);
    // When the D1 raw table was queried, it is the source of truth.  Do not
    // fall back to a snapshot-calculated value for a missing code: that value
    // may have been calculated across a source gap.  The optional argument is
    // kept for legacy fixtures that do not have a raw table at all.
    const value = hasOfficialChanges
      ? officialChanges.get(key)
      : item.periods["1d"];
    if (value === null || value === undefined || !Number.isFinite(value)) continue;
    const existing = rows.get(key);
    const row: StateInstrument = {
      code: item.code,
      name: item.name,
      market: item.market ?? "SSE",
      category: item.category,
      groupType: item.groupType,
      assetClass: item.assetClass,
      classificationSource: item.classificationSource,
      classificationConfidence: item.classificationConfidence,
      classificationRule: item.classificationRule,
      includeInGroup: item.includeInGroup,
      values: existing?.values ?? Array<number | null>(dates.length).fill(null),
      cashValues: existing?.cashValues ?? Array<number | null>(dates.length).fill(null),
    };
    row.values[currentIndex] = Number(value.toFixed(6));
    const cashValue = item.estimatedCashPeriods?.["1d"];
    if (row.cashValues) {
      row.cashValues[currentIndex] =
        typeof cashValue === "number" && Number.isFinite(cashValue)
          ? Number(cashValue.toFixed(2))
          : null;
    }
    rows.set(key, row);
  }

  const instruments = [...rows.values()]
    .filter((item) => item.values.some((value) => value !== null))
    .sort((left, right) =>
      `${left.market}:${left.code}`.localeCompare(`${right.market}:${right.code}`),
    );
  const sourceSnapshots = [
    ...(previous?.sourceSnapshots ?? []).filter((item) => dates.includes(item.date)),
  ].filter((item) => item.date !== snapshot.asOf);
  sourceSnapshots.push({ date: snapshot.asOf, sha256: sourceSnapshotSha256 });
  sourceSnapshots.sort((left, right) => left.date.localeCompare(right.date));
  const populatedValues = instruments.reduce(
    (total, item) => total + item.values.filter((value) => value !== null).length,
    0,
  );
  return {
    version: ETF_FLOW_STATE_VERSION,
    classificationVersion: ETF_CLASSIFICATION_VERSION,
    calculationMethod: "sum_of_daily_official_share_changes",
    cashCalculationMethod: "sum_of_daily_share_changes_times_same_day_nav",
    generatedAt: new Date().toISOString(),
    dates,
    instruments,
    sourceSnapshots,
    quality: {
      snapshotCount: dates.length,
      instrumentCount: instruments.length,
      populatedValues,
      rejectedRows: previous?.quality.rejectedRows ?? 0,
      duplicateRows: previous?.quality.duplicateRows ?? 0,
      status:
        (previous?.quality.rejectedRows ?? 0) === 0 &&
        (previous?.quality.duplicateRows ?? 0) === 0
          ? "pass"
          : "conditional_pass",
    },
  };
}

function periodIndexes(
  dates: string[],
  asOf: string,
): Partial<Record<FlowPeriod, number[]>> {
  const eligible = dates
    .map((date, index) => ({ date, index }))
    .filter((item) => item.date <= asOf);
  const output: Partial<Record<FlowPeriod, number[]>> = {};
  for (const [period, count] of Object.entries(PERIOD_WINDOWS) as Array<
    [FlowPeriod, number]
  >) {
    if (eligible.length >= count) {
      output[period] = eligible.slice(-count).map((item) => item.index);
    }
  }
  const year = asOf.slice(0, 4);
  const yearRows = eligible.filter((item) => item.date.startsWith(year));
  const hasPriorYear = eligible.some((item) => item.date.slice(0, 4) < year);
  if (hasPriorYear && yearRows.length) {
    output.ytd = yearRows.map((item) => item.index);
  }
  return output;
}

function sumValues(
  values: Array<number | null>,
  indexes: number[],
): { value: number | null; coverage: number } {
  const observed = indexes
    .map((index) => values[index])
    .filter((value): value is number => value !== null && value !== undefined);
  return {
    // A period is valid only when every expected official trading-day point
    // is present.  Returning a partial sum makes a 3d/1w number look complete
    // and was the root of the repeated cross-gap values in the radar.
    value: observed.length === indexes.length && indexes.length > 0
      ? Number(observed.reduce((sum, value) => sum + value, 0).toFixed(6))
      : null,
    coverage: observed.length,
  };
}

function displayClassification(item: StateInstrument) {
  const unclassified =
    item.classificationRule === "unmatched" ||
    item.classificationSource === "unmatched" ||
    (item.includeInGroup === false && item.category === "其他");
  return {
    category: unclassified ? UNCLASSIFIED_CATEGORY : item.category,
    groupType: unclassified ? UNCLASSIFIED_GROUP_TYPE : item.groupType,
    // Eligible ETFs are all represented in exactly one visible bucket.  The
    // raw classifier fields above remain intact for audit and future rule
    // improvements; this flag describes the display universe only.
    includeInGroup: true,
    unclassified,
  };
}

function deriveEtfFlowHeadline(instruments: EtfTerminalInstrument[]) {
  const changes = instruments.flatMap((item) => {
    const value = item.periods["1d"];
    return typeof value === "number" && Number.isFinite(value) ? [{ item, value }] : [];
  });
  const rows = (ascending: boolean) =>
    [...changes]
      .sort((left, right) => ascending ? left.value - right.value : right.value - left.value)
      .slice(0, 5)
      .map(({ item, value }) => ({ code: item.code, name: item.name, value: Number(value.toFixed(2)) }));
  const net = changes.reduce((sum, row) => sum + row.value, 0);
  return {
    flows: { inflow: rows(false), outflow: rows(true) },
    summary: {
      net,
      increasers: changes.filter((row) => row.value > 0).length,
      decreasers: changes.filter((row) => row.value < 0).length,
    },
  };
}

export function enrichSnapshotWithEtfFlowState(
  snapshot: DashboardSnapshot,
  state: EtfFlowState | null,
): DashboardSnapshot {
  const terminal = snapshot.etfTerminal;
  if (!terminal || !state || !state.dates.some((date) => date <= snapshot.asOf)) {
    return snapshot;
  }
  const indexes = periodIndexes(state.dates, snapshot.asOf);
  const availablePeriods = PERIOD_ORDER.filter((period) => indexes[period]?.length);
  if (!availablePeriods.length) return snapshot;
  const stateRows = new Map(
    state.instruments.map((item) => [instrumentKey(item), item]),
  );
  const instruments: EtfTerminalInstrument[] = terminal.instruments.map((item) => {
    const stateItem = stateRows.get(instrumentKey(item));
    if (!stateItem) return item;
    const display = displayClassification(stateItem);
    const periods: EtfTerminalInstrument["periods"] = {};
    const estimatedCashPeriods: NonNullable<
      EtfTerminalInstrument["estimatedCashPeriods"]
    > = {};
    const cashPeriodCoverage: Partial<Record<FlowPeriod, number>> = {};
    const periodCoverage: Partial<Record<FlowPeriod, number>> = {};
    for (const period of availablePeriods) {
      const result = sumValues(stateItem.values, indexes[period] ?? []);
      periods[period] = result.value;
      periodCoverage[period] = result.coverage;
      const cashResult = stateItem.cashValues
        ? sumValues(stateItem.cashValues, indexes[period] ?? [])
        : { value: null, coverage: 0 };
      const fallbackCash = item.estimatedCashPeriods?.[period];
      const expectedCoverage = indexes[period]?.length ?? 0;
      const fallbackCoverage = item.cashPeriodCoverage?.[period] ?? 0;
      const fallbackIsComplete =
        typeof fallbackCash === "number" &&
        Number.isFinite(fallbackCash) &&
        expectedCoverage > 0 &&
        fallbackCoverage === expectedCoverage;
      cashPeriodCoverage[period] = cashResult.coverage || (fallbackIsComplete ? fallbackCoverage : 0);
      // Never multiply a multi-day share change by the latest NAV.  The
      // persisted state carries one cash estimate per trading day, already
      // valued with that day's official NAV.  Legacy snapshots can retain an
      // already-calculated value as a compatibility fallback, but no new
      // value is inferred from the current NAV here.
      estimatedCashPeriods[period] =
        cashResult.value ?? (fallbackIsComplete ? fallbackCash : null);
    }
    return {
      ...item,
      category: display.category,
      groupType: display.groupType,
      assetClass: stateItem.assetClass,
      classificationSource: stateItem.classificationSource,
      classificationConfidence: stateItem.classificationConfidence,
      classificationRule: stateItem.classificationRule,
      includeInGroup: display.includeInGroup,
      periods,
      estimatedCashPeriods,
      cashPeriodCoverage,
      coverage: Math.max(0, ...Object.values(periodCoverage)),
      periodCoverage,
    };
  });

  const groups = new Map<string, EtfTerminalData["groupPeriods"][number]>();
  for (const item of instruments) {
    const group = groups.get(item.category) ?? {
      name: item.category,
      groupType: item.groupType,
      periods: {},
      estimatedCashPeriods: {},
      estimatedCashCoverage: {},
      componentCount: 0,
      comparableCount: {},
    };
    group.componentCount = (group.componentCount ?? 0) + 1;
    for (const period of availablePeriods) {
      const value = item.periods[period];
      if (value !== null && value !== undefined) {
        group.periods[period] = Number(
          ((group.periods[period] ?? 0) + value).toFixed(6),
        );
        group.comparableCount![period] =
          (group.comparableCount?.[period] ?? 0) + 1;
      }
      const cash = item.estimatedCashPeriods?.[period];
      if (cash !== null && cash !== undefined) {
        group.estimatedCashPeriods![period] = Number(
          ((group.estimatedCashPeriods?.[period] ?? 0) + cash).toFixed(2),
        );
      }
      group.estimatedCashCoverage![period] =
        (group.estimatedCashCoverage?.[period] ?? 0) +
        (item.cashPeriodCoverage?.[period] ?? 0);
    }
    groups.set(item.category, group);
  }
  const groupPeriods = [...groups.values()].map((group) => {
    for (const period of availablePeriods) {
      if (!(group.estimatedCashCoverage?.[period] ?? 0)) {
        group.estimatedCashPeriods![period] = null;
      }
    }
    return group;
  }).sort(
    (left, right) =>
      Math.abs(right.periods["1d"] ?? 0) - Math.abs(left.periods["1d"] ?? 0),
  );

  const eligibleDates = state.dates
    .map((date, index) => ({ date, index }))
    .filter((item) => item.date <= snapshot.asOf)
    .slice(-60);
  const dailySeries = eligibleDates.map(({ date, index }) => {
    const observed = state.instruments
      .map((item) => item.values[index])
      .filter((value): value is number => value !== null && value !== undefined);
    const observedCash = state.instruments
      .map((item) => item.cashValues?.[index])
      .filter((value): value is number => value !== null && value !== undefined);
    return {
      date: date.slice(5),
      value: Number(observed.reduce((sum, value) => sum + value, 0).toFixed(6)),
      coverage: observed.length,
      cashValue: observedCash.length
        ? Number(observedCash.reduce((sum, value) => sum + value, 0).toFixed(2))
        : null,
      cashCoverage: observedCash.length,
    };
  });
  const categoryCounts: Record<string, number> = {};
  for (const item of instruments) {
    categoryCounts[item.category] = (categoryCounts[item.category] ?? 0) + 1;
  }
  const periodComparable = instruments.filter(
    (item) => item.periods["1d"] !== null && item.periods["1d"] !== undefined,
  ).length;
  const cashFlowEligible = instruments.filter(
    (item) => item.estimatedCashPeriods?.["1d"] !== null && item.estimatedCashPeriods?.["1d"] !== undefined,
  ).length;
  const unclassified = instruments.filter(
    (item) => item.category === UNCLASSIFIED_CATEGORY,
  ).length;
  const etfHeadline = deriveEtfFlowHeadline(instruments);
  const metrics = snapshot.metrics.map((metric) =>
    metric.id !== "etf"
      ? metric
      : {
          ...metric,
          eyebrow: "沪深 ETF 份额变化",
          value: `${etfHeadline.summary.net >= 0 ? "+" : ""}${etfHeadline.summary.net.toFixed(2)}`,
          delta: `${etfHeadline.summary.increasers} 只增加 · ${etfHeadline.summary.decreasers} 只减少`,
          tone: etfHeadline.summary.net >= 0 ? "positive" : "negative",
          basis: metric.basis
            ? { ...metric.basis, detail: "上交所与深交所当日官方份额 − 前一交易日官方份额" }
            : metric.basis,
        },
  );

  return {
    ...snapshot,
    etfFlows: etfHeadline.flows,
    metrics,
    etfTerminal: {
      ...terminal,
      instruments,
      groupPeriods,
      dailySeries,
      availablePeriods,
      classificationVersion: ETF_CLASSIFICATION_VERSION,
      calculation:
        "各交易日官方份额变化逐日累计；低置信度产品统一进入‘未归类’，不强行归入行业；金额估算=逐日份额变化×对应交易日官方单位净值后累计（单位：亿元）",
      scope: "SSE+SZSE",
      cashFlow: {
        available: cashFlowEligible > 0,
        unit: "亿元",
        method: "sum_of_daily_share_changes_times_same_day_nav",
        eligibleInstruments: cashFlowEligible,
        totalInstruments: instruments.length,
        coverageDate: snapshot.asOf,
        warning: "缺少对应日期官方单位净值的 ETF 不计入金额汇总",
      },
      historyCoverage: {
        from: state.dates[0],
        to: state.dates.at(-1) ?? snapshot.asOf,
        tradingDays: state.dates.length,
        stateVersion: state.version,
      },
      universe: terminal.universe
        ? {
            ...terminal.universe,
            categoryCounts,
            periodComparable,
            cashFlowEligible,
            classified: instruments.length - unclassified,
            unclassified,
          }
        : terminal.universe,
    },
  };
}
