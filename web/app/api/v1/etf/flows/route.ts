import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    unit: "100m shares",
    scope: "SSE headline; SSE and SZSE terminal universe",
    methodology: "adjacent trading-day share difference",
    evidence: {
      kind: "calculated",
      source: "official exchange share snapshots",
      formula: "current shares - previous trading-day shares",
      isCashFlow: false,
    },
    limitations: ["SZSE current file has no source-published statistical date"],
    flows: dashboardSnapshot.etfFlows,
    terminal: dashboardSnapshot.etfTerminal ?? null,
  });
}
