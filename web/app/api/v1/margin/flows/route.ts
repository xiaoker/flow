import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    currency: "CNY",
    unit: "100m yuan",
    series: dashboardSnapshot.marginSeries,
  });
}
