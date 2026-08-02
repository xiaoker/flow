import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

type EtfHistoryRow = {
  trade_date: string;
  market: "SSE" | "SZSE";
  instrument_name: string;
  shares: number;
};

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
    const history = rows.map((row, index) => ({
      date: row.trade_date,
      shares: Number((row.shares / 100_000_000).toFixed(6)),
      delta: index === 0
        ? null
        : Number(((row.shares - rows[index - 1].shares) / 100_000_000).toFixed(6)),
    }));
    return NextResponse.json({
      code,
      market,
      name: rows.at(-1)?.instrument_name ?? code,
      unit: "亿份",
      available: history.length > 0,
      history,
    }, {
      headers: { "Cache-Control": "public, max-age=60, s-maxage=60" },
    });
  } catch {
    return NextResponse.json({ history: [], available: false }, { status: 503 });
  }
}
