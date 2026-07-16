import { NextResponse } from "next/server";
import { getDashboardSnapshot } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dashboardSnapshot = await getDashboardSnapshot();
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    northboundDisclosure: "turnover_only",
    evidence: {
      southbound: { kind: "calculated", formula: "official buy turnover - official sell turnover" },
      northbound: { kind: "official", field: "total turnover", directionAvailable: false },
    },
    channels: dashboardSnapshot.connect,
    series: dashboardSnapshot.connectSeries ?? [],
  });
}
