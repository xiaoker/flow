import { NextResponse } from "next/server";
import { dashboardSnapshot } from "@/app/lib/dashboard-data";

export const dynamic = "force-static";

export async function GET() {
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    unit: "100m shares",
    scope: "SSE only",
    methodology: "adjacent trading-day share difference",
    flows: dashboardSnapshot.etfFlows,
  });
}
