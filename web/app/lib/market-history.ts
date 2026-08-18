import type { DashboardSnapshot } from "./dashboard-data";

export const MARKET_HISTORY_MAX_DATES = 750;

export type MarketHistoryRow = {
  trade_date: string;
  margin_balance: number | null;
  margin_change: number | null;
  financing_buy: number | null;
  lending_balance: number | null;
  lending_sell_volume: number | null;
  total_balance: number | null;
  sse_balance: number | null;
  szse_balance: number | null;
  bse_balance: number | null;
  margin_scope_version: "sse_szse_v1" | "sse_szse_bse_v2" | string;
  southbound_net: number | null;
  southbound_turnover: number | null;
  southbound_sse_net: number | null;
  southbound_szse_net: number | null;
  northbound_turnover: number | null;
  northbound_etf_turnover: number | null;
  northbound_trade_count: number | null;
  margin_available: number;
  connect_available: number;
  source_snapshot_sha256: string;
  observed_at: string;
};

export type MarketHistoryPoint = Omit<
  MarketHistoryRow,
  "source_snapshot_sha256" | "observed_at"
>;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function matchesSnapshotDate(value: unknown, asOf: string): boolean {
  return value === asOf || value === asOf.slice(5);
}

export function extractMarketHistoryPoint(
  snapshot: DashboardSnapshot,
): MarketHistoryPoint {
  const margin = [...snapshot.marginSeries]
    .reverse()
    .find((item) => matchesSnapshotDate(item.date, snapshot.asOf));
  const connect = [...(snapshot.connectSeries ?? [])]
    .reverse()
    .find((item) => matchesSnapshotDate(item.date, snapshot.asOf));

  return {
    trade_date: snapshot.asOf,
    margin_balance: finite(margin?.balance),
    margin_change: finite(margin?.change),
    financing_buy: finite(margin?.financingBuy),
    lending_balance: finite(margin?.lendingBalance),
    lending_sell_volume: finite(margin?.lendingSellVolume),
    total_balance: finite(margin?.totalBalance),
    sse_balance: finite(margin?.sseBalance),
    szse_balance: finite(margin?.szseBalance),
    bse_balance: finite(margin?.bseBalance),
    southbound_net: finite(connect?.southboundNet),
    southbound_turnover: finite(connect?.southboundTurnover),
    southbound_sse_net: finite(connect?.southboundSseNet),
    southbound_szse_net: finite(connect?.southboundSzseNet),
    northbound_turnover: finite(connect?.northboundTurnover),
    northbound_etf_turnover: finite(connect?.northboundEtfTurnover),
    northbound_trade_count: finite(connect?.northboundTradeCount),
    margin_available: margin ? 1 : 0,
    connect_available: connect ? 1 : 0,
    margin_scope_version: margin?.marginScopeVersion === "sse_szse_bse_v2"
      ? "sse_szse_bse_v2"
      : "sse_szse_v1",
  };
}

function validHistoryRow(row: MarketHistoryRow): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(row.trade_date) &&
    (row.margin_available === 0 || row.margin_available === 1) &&
    (row.connect_available === 0 || row.connect_available === 1)
  );
}

export function enrichSnapshotWithMarketHistory(
  snapshot: DashboardSnapshot,
  rows: MarketHistoryRow[],
): DashboardSnapshot {
  const eligible = rows
    .filter((row) => validHistoryRow(row) && row.trade_date <= snapshot.asOf)
    .sort((left, right) => left.trade_date.localeCompare(right.trade_date))
    .slice(-MARKET_HISTORY_MAX_DATES);
  if (!eligible.length) return snapshot;

  const marginSeries: DashboardSnapshot["marginSeries"] = eligible.flatMap((row) =>
    row.margin_available && row.margin_balance !== null
      ? [{
          date: row.trade_date,
          balance: row.margin_balance,
          label: `${(row.margin_balance / 10_000).toFixed(4)} 万亿`,
          change: row.margin_change,
          financingBuy: row.financing_buy ?? undefined,
          lendingBalance: row.lending_balance ?? undefined,
          lendingSellVolume: row.lending_sell_volume ?? undefined,
          totalBalance: row.total_balance ?? undefined,
          sseBalance: row.sse_balance ?? undefined,
          szseBalance: row.szse_balance ?? undefined,
          bseBalance: row.margin_scope_version === "sse_szse_bse_v2"
            ? row.bse_balance ?? undefined
            : undefined,
          marginScopeVersion: row.margin_scope_version || "sse_szse_v1",
        }]
      : [],
  );
  const connectSeries: NonNullable<DashboardSnapshot["connectSeries"]> =
    eligible.flatMap((row) =>
      row.connect_available &&
      row.southbound_net !== null &&
      row.northbound_turnover !== null
        ? [{
            date: row.trade_date,
            southboundNet: row.southbound_net,
            southboundTurnover: row.southbound_turnover ?? 0,
            southboundSseNet: row.southbound_sse_net ?? 0,
            southboundSzseNet: row.southbound_szse_net ?? 0,
            northboundTurnover: row.northbound_turnover,
            northboundEtfTurnover: row.northbound_etf_turnover ?? 0,
            northboundTradeCount: row.northbound_trade_count ?? 0,
          }]
        : [],
    );

  return {
    ...snapshot,
    marginSeries: marginSeries.length ? marginSeries : snapshot.marginSeries,
    connectSeries: connectSeries.length ? connectSeries : snapshot.connectSeries,
    marketHistoryCoverage: {
      archiveFrom: eligible[0].trade_date,
      archiveTo: eligible.at(-1)?.trade_date ?? snapshot.asOf,
      archiveDates: eligible.length,
      margin: {
        from: marginSeries.at(0)?.date ?? null,
        to: marginSeries.at(-1)?.date ?? null,
        points: marginSeries.length,
      },
      connect: {
        from: connectSeries.at(0)?.date ?? null,
        to: connectSeries.at(-1)?.date ?? null,
        points: connectSeries.length,
      },
    },
  };
}
