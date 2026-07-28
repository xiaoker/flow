import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    currency: "CNY",
    unit: "100m yuan",
    evidence: {
      kind: "official",
      source: "SSE and SZSE margin trading summaries",
      interpretation: "balance and turnover data; not net capital flow",
    },
    coverage: dashboardSnapshot.marketHistoryCoverage?.margin ?? {
      from: dashboardSnapshot.marginSeries.at(0)?.date ?? null,
      to: dashboardSnapshot.marginSeries.at(-1)?.date ?? null,
      points: dashboardSnapshot.marginSeries.length,
    },
    series: dashboardSnapshot.marginSeries,
  });
}
