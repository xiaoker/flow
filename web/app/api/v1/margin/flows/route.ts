import { NextResponse } from "next/server";
import { dashboardSnapshot } from "@/app/lib/dashboard-data";

export const dynamic = "force-static";

export async function GET() {
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    currency: "CNY",
    unit: "100m yuan",
    series: dashboardSnapshot.marginSeries,
  });
}
