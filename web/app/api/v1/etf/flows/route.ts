import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    unit: "100m shares",
    scope: "SSE share-change headline; eligible SSE and SZSE ETF terminal universe",
    methodology: "versioned primary classification; adjacent trading-day official share difference; low-confidence ETFs remain in the unclassified reconciliation bucket",
    evidence: {
      kind: "calculated",
      source: "official exchange share snapshots",
      formula: "current shares - previous trading-day shares",
      isCashFlow: false,
    },
    limitations: [
      "The headline metric is SSE-only; the ETF terminal is the combined SSE+SZSE state when the hash-verified history is available",
      "Equity group assignment remains a conservative name inference until tracked-index metadata is collected",
      "Share change is not cash flow; estimated cash requires both NAV and comparable shares",
    ],
    flows: dashboardSnapshot.etfFlows,
    terminal: dashboardSnapshot.etfTerminal ?? null,
  });
}
