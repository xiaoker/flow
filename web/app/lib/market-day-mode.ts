/**
 * Single control contract for the market-day probe.
 *
 * The new mode is deliberately explicit.  The old boolean is accepted only
 * as a compatibility input: 0 means OFF and 1 means ENFORCE, so a legacy
 * production value cannot silently become an observing control.  If both
 * inputs are present they must describe the same mode.
 */
export const MARKET_DAY_PROBE_MODES = ["OFF", "OBSERVE", "ENFORCE"] as const;
export type MarketDayProbeMode = (typeof MARKET_DAY_PROBE_MODES)[number];

export type MarketDayProbeModeResolution = {
  mode: MarketDayProbeMode;
  source: "default" | "mode" | "legacy" | "both";
};

function normalizeMode(value: string): MarketDayProbeMode | null {
  const normalized = value.trim().toUpperCase();
  return MARKET_DAY_PROBE_MODES.includes(normalized as MarketDayProbeMode)
    ? normalized as MarketDayProbeMode
    : null;
}

function normalizeLegacy(value: string): MarketDayProbeMode | null {
  const normalized = value.trim().toLowerCase();
  if (normalized === "0" || normalized === "false") return "OFF";
  if (normalized === "1" || normalized === "true") return "ENFORCE";
  return null;
}

/** Resolve the new mode and the time-limited legacy boolean without defaults that hide conflicts. */
export function resolveMarketDayProbeMode(input: {
  mode?: string;
  legacyEnabled?: string;
}): MarketDayProbeModeResolution {
  const modeValue = input.mode?.trim() ?? "";
  const legacyValue = input.legacyEnabled?.trim() ?? "";
  const explicitMode = modeValue ? normalizeMode(modeValue) : null;
  const legacyMode = legacyValue ? normalizeLegacy(legacyValue) : null;

  if (modeValue && !explicitMode) throw new Error("market_day_probe_mode_invalid");
  if (legacyValue && !legacyMode) throw new Error("market_day_probe_legacy_flag_invalid");
  if (explicitMode && legacyMode && explicitMode !== legacyMode) {
    throw new Error("market_day_probe_mode_conflict");
  }
  if (explicitMode && legacyMode) return { mode: explicitMode, source: "both" };
  if (explicitMode) return { mode: explicitMode, source: "mode" };
  if (legacyMode) return { mode: legacyMode, source: "legacy" };
  return { mode: "OFF", source: "default" };
}

