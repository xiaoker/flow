import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    observedAt: dashboardSnapshot.observedAt,
    gate: dashboardSnapshot.gate,
    mode: dashboardSnapshot.mode,
    quality: dashboardSnapshot.quality,
    domains: dashboardSnapshot.dataStatus,
    marketHistoryCoverage: dashboardSnapshot.marketHistoryCoverage ?? null,
  });
}
