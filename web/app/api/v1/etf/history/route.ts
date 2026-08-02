import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import { loadEtfFlowState } from "@/app/lib/etf-flow-state";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

type EtfHistoryRow = {
  trade_date: string;
  market: "SSE" | "SZSE";
  instrument_name: string;
  shares: number;
};

type EtfHistoryPoint = {
  date: string;
  shares: number;
  delta: number | null;
};

function replayStateHistory(
  state: Awaited<ReturnType<typeof loadEtfFlowState>>,
  code: string,
  market: "SSE" | "SZSE",
  currentShares: number | null,
): EtfHistoryPoint[] {
  if (!state || currentShares === null) return [];
  const item = state.instruments.find((row) => row.code === code && row.market === market);
  if (!item) return [];
  const lastIndex = state.dates.length - 1;
  if (lastIndex < 0 || item.values[lastIndex] === null) return [];
  let running = currentShares;
  const points: EtfHistoryPoint[] = [];
  for (let index = lastIndex; index >= 0; index -= 1) {
    if (index < lastIndex) {
      const nextDelta = item.values[index + 1];
      if (nextDelta === null || nextDelta === undefined) break;
      running = Number((running - nextDelta).toFixed(6));
    }
    points.unshift({
      date: state.dates[index],
      shares: running,
      delta: index === 0 ? null : item.values[index],
    });
  }
  return points;
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code") ?? "";
  const market = url.searchParams.get("market") ?? "";
  if (!/^\d{6}$/.test(code) || (market !== "SSE" && market !== "SZSE")) {
    return NextResponse.json({ error: "invalid_etf_identity" }, { status: 400 });
  }

  const db = (await runtimeEnv()).DB;
  if (!db) return NextResponse.json({ history: [], available: false });

  try {
    const result = await db
      .prepare(
        `WITH ranked AS (
           SELECT trade_date, market, instrument_name, shares,
                  ROW_NUMBER() OVER (
                    PARTITION BY trade_date, market, instrument_code
                    ORDER BY observed_at DESC, release_id DESC
                  ) AS row_number
           FROM etf_daily
           WHERE instrument_code = ? AND market = ?
         )
         SELECT trade_date, market, instrument_name, shares
         FROM ranked
         WHERE row_number = 1 AND trade_date <= date('now', '+1 day')
         ORDER BY trade_date DESC
         LIMIT 260`,
      )
      .bind(code, market)
      .all<EtfHistoryRow>();
    const rows = [...(result.results ?? [])]
      .filter((row) => ISO_DATE.test(row.trade_date) && Number.isFinite(row.shares))
      .reverse();
    const rawHistory = rows.map((row, index) => ({
      date: row.trade_date,
      shares: Number((row.shares / 100_000_000).toFixed(6)),
      delta: index === 0
        ? null
        : Number(((row.shares - rows[index - 1].shares) / 100_000_000).toFixed(6)),
    }));
    const state = await loadEtfFlowState((await runtimeEnv()).RAW_BUCKET);
    const stateAsOf = state?.dates.at(-1) ?? "";
    const currentRow = rows.filter((row) => row.trade_date <= stateAsOf).at(-1) ?? rows.at(-1);
    const replayed = replayStateHistory(
      state,
      code,
      market,
      currentRow ? Number((currentRow.shares / 100_000_000).toFixed(6)) : null,
    );
    const history = replayed.length >= 2
      ? [...replayed, ...rawHistory.filter((point) => point.date > replayed.at(-1)!.date)]
      : rawHistory;
    return NextResponse.json({
      code,
      market,
      name: rows.at(-1)?.instrument_name ?? code,
      unit: "亿份",
      available: history.length > 0,
      basis: replayed.length >= 2 ? "official_daily_change_replay" : "official_daily_share_snapshots",
      history,
    }, {
      headers: { "Cache-Control": "public, max-age=60, s-maxage=60" },
    });
  } catch {
    return NextResponse.json({ history: [], available: false }, { status: 503 });
  }
}
