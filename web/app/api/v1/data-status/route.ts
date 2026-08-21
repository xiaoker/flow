import { NextResponse } from "next/server";
import { getActiveReleaseManifest } from "@/app/lib/release-store";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const [dashboardSnapshot, active] = await Promise.all([
    getDashboardSnapshot(),
    getActiveReleaseManifest(),
  ]);
  return NextResponse.json({
    releaseId: active?.releaseId ?? null,
    releaseAsOf: active?.asOf ?? dashboardSnapshot.asOf,
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
    sourceReadiness: dashboardSnapshot.sourceReadiness ?? null,
    domains: dashboardSnapshot.dataStatus,
    marketHistoryCoverage: dashboardSnapshot.marketHistoryCoverage ?? null,
  });
}
