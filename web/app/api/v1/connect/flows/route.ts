import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    northboundDisclosure: "turnover_only",
    channels: dashboardSnapshot.connect,
    series: dashboardSnapshot.connectSeries ?? [],
  });
}
