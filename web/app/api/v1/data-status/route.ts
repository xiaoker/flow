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
    publication: dashboardSnapshot.publication ?? {
      status: "confirmed",
      targetAsOf: dashboardSnapshot.asOf,
      pendingDomains: [],
      detail: "兼容历史完整快照",
    },
    domains: dashboardSnapshot.dataStatus,
    marketHistoryCoverage: dashboardSnapshot.marketHistoryCoverage ?? null,
  });
}
