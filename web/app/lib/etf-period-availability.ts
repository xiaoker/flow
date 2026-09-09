import type { EtfTerminalData, FlowPeriod } from "./dashboard-data";

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

/**
 * Resolve the period windows declared by the published ETF terminal.
 *
 * A declared window is a valid query even when its current release has no
 * comparable observations yet.  Keep this separate from
 * `getEffectiveEtfSharePeriods`, which is deliberately evidence-based and is
 * used for coverage/status messaging.
 */
export function getSelectableEtfSharePeriods(
  terminal: EtfTerminalData,
): FlowPeriod[] {
  const declared = terminal.availablePeriods?.filter((period) =>
    PERIOD_ORDER.includes(period),
  ) ?? [];
  if (declared.length) {
    return PERIOD_ORDER.filter((period) => declared.includes(period));
  }

  const structural = PERIOD_ORDER.filter(
    (period) =>
      terminal.reconciliation?.[period] !== undefined ||
      terminal.instruments.some((item) =>
        Object.prototype.hasOwnProperty.call(item.periods, period),
      ),
  );
  return structural.length ? structural : ["1d"];
}

/**
 * Resolve the ETF share periods that can actually be displayed.
 *
 * Published overview artifacts intentionally omit the full instrument matrix
 * and expose reconciliation counters instead.  The producer's declared
 * `availablePeriods` is a structural date-window list, so it can contain a
 * period whose every ETF value is null when a historical snapshot is missing.
 * Use numeric instrument values when the full Radar matrix is present; use
 * compact reconciliation evidence for the default overview.  A legacy
 * artifact with neither evidence remains readable using its declared list.
 */
export function getEffectiveEtfSharePeriods(
  terminal: EtfTerminalData,
): FlowPeriod[] {
  const candidates = getSelectableEtfSharePeriods(terminal);

  if (terminal.instruments.length) {
    const instrumentPeriods = candidates.filter((period) =>
      terminal.instruments.some(
        (item) => typeof item.periods[period] === "number",
      ),
    );
    if (instrumentPeriods.length) return instrumentPeriods;
  }

  const reconciliationPeriods = candidates.filter(
    (period) =>
      typeof terminal.reconciliation?.[period]?.shares?.comparable === "number",
  );
  if (reconciliationPeriods.length) {
    return reconciliationPeriods.filter(
      (period) =>
        (terminal.reconciliation?.[period]?.shares?.comparable ?? 0) > 0,
    );
  }

  return candidates;
}
