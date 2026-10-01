/** Additive PCF v2 control-plane contracts. Production readers remain on the legacy path until the capability gate. */

export const PCF_CAPTURE_CALCULATION_VERSION = "pcf-capture-v2" as const;
export const PCF_SOURCE_WINDOW_VERSION = "sse-pcf-exact-v1" as const;
export const PCF_QUALIFICATION_VERSION = "pcf-qualification-v1" as const;

export type PcfBaselineStatus =
  | "COMPARABLE_CHANGED"
  | "COMPARABLE_UNCHANGED"
  | "NEW_BASELINE_MISSING"
  | "MISSING_CURRENT"
  | "INVALID_SHARE";

export type PcfCaptureJobStatus =
  | "PLANNED"
  | "RUNNING"
  | "PARTIAL"
  | "RATE_LIMITED"
  | "EXPIRED_EXACT_WINDOW"
  | "BLOCKED_QUALITY"
  | "BLOCKED_UNIVERSE_ANOMALY"
  | "READY_NO_CHANGE"
  | "FINALIZING"
  | "FINALIZE_FAILED"
  | "READY";

export type PcfCaptureItemState =
  | "NOT_REQUIRED"
  | "PENDING"
  | "RUNNING"
  | "RETRYABLE"
  | "CAPTURED"
  | "VALID"
  | "BLOCKED";

export interface PcfCapturePlanItem {
  market: string;
  etfCode: string;
  etfName: string;
  currentShares: number | null;
  previousShares: number | null;
  shareDelta: number | null;
  baselineStatus: PcfBaselineStatus;
  required: boolean;
  state: PcfCaptureItemState;
}

export interface PcfCapturePlan {
  targetDate: string;
  previousDate: string;
  calculationVersion: typeof PCF_CAPTURE_CALCULATION_VERSION;
  hardLimit: number;
  status: "PLANNED" | "READY_NO_CHANGE" | "BLOCKED_QUALITY" | "BLOCKED_UNIVERSE_ANOMALY";
  sourceWindow: {
    version: typeof PCF_SOURCE_WINDOW_VERSION;
    provider: "sse";
    dataset: "etf_pcf";
    probeCode: string;
    targetDate: string;
    previousDate: string;
    exactDateRule: string;
    windowClass: "official_exact";
  };
  sourceEvidence: Record<string, unknown>;
  eligibility: {
    status: "ELIGIBLE" | "BLOCKED";
    basis: string;
    setSha256: string;
    codes: string[];
  };
  qualification: {
    version: typeof PCF_QUALIFICATION_VERSION;
    status: "PENDING" | "QUALIFIED" | "BLOCKED";
    captureDate: string;
    qualificationDate: string | null;
    eligibleSetSha256: string;
    evidence: Record<string, unknown>;
  };
  planSha256: string;
  items: PcfCapturePlanItem[];
  counts: {
    expected: number;
    required: number;
    unchanged: number;
    newBaselineMissing: number;
    missingCurrent: number;
    invalidShare: number;
  };
}

export interface PcfManifestContract {
  schemaVersion: 2;
  dataset: "sse_etf_pcf";
  calculationVersion: typeof PCF_CAPTURE_CALCULATION_VERSION;
  tradeDate: string;
  previousDate: string;
  jobId: string;
  planSha256: string;
  source?: Record<string, unknown>;
  universe: Record<string, number>;
  coverage: Record<string, number>;
  artifacts: Record<string, { key: string; sha256: string; rows?: number; members?: number }>;
  quality: Record<string, number>;
  observedAt: string;
  sourceWindow?: PcfCapturePlan["sourceWindow"];
  sourceEvidence?: Record<string, unknown>;
  eligibility?: PcfCapturePlan["eligibility"];
  qualification?: PcfCapturePlan["qualification"];
}

const SHA256 = /^[0-9a-f]{64}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export function isPcfCapturePlan(value: unknown): value is PcfCapturePlan {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const plan = value as Record<string, unknown>;
  const counts = plan.counts;
  const sourceWindow = isRecord(plan.sourceWindow) ? plan.sourceWindow : null;
  const eligibility = isRecord(plan.eligibility) ? plan.eligibility : null;
  const qualification = isRecord(plan.qualification) ? plan.qualification : null;
  return (
    plan.calculationVersion === PCF_CAPTURE_CALCULATION_VERSION &&
    typeof plan.targetDate === "string" && ISO_DATE.test(plan.targetDate) &&
    typeof plan.previousDate === "string" && ISO_DATE.test(plan.previousDate) &&
    typeof plan.planSha256 === "string" && SHA256.test(plan.planSha256) &&
    Array.isArray(plan.items) &&
    sourceWindow !== null && sourceWindow.version === PCF_SOURCE_WINDOW_VERSION &&
    typeof sourceWindow.targetDate === "string" && sourceWindow.targetDate === plan.targetDate &&
    typeof sourceWindow.previousDate === "string" && sourceWindow.previousDate === plan.previousDate &&
    eligibility !== null && typeof eligibility.setSha256 === "string" && SHA256.test(eligibility.setSha256) &&
    Array.isArray(eligibility.codes) &&
    qualification !== null && qualification.version === PCF_QUALIFICATION_VERSION &&
    qualification.captureDate === plan.targetDate &&
    counts !== null && typeof counts === "object" && !Array.isArray(counts)
  );
}

export function isPcfManifestContract(value: unknown): value is PcfManifestContract {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const manifest = value as Record<string, unknown>;
  return (
    manifest.schemaVersion === 2 &&
    manifest.dataset === "sse_etf_pcf" &&
    manifest.calculationVersion === PCF_CAPTURE_CALCULATION_VERSION &&
    typeof manifest.tradeDate === "string" && ISO_DATE.test(manifest.tradeDate) &&
    typeof manifest.previousDate === "string" && ISO_DATE.test(manifest.previousDate) &&
    typeof manifest.planSha256 === "string" && SHA256.test(manifest.planSha256) &&
    typeof manifest.observedAt === "string" &&
    manifest.universe !== null && typeof manifest.universe === "object" &&
    manifest.coverage !== null && typeof manifest.coverage === "object" &&
    manifest.artifacts !== null && typeof manifest.artifacts === "object"
  );
}
