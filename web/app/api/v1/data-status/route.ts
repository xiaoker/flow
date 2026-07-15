import { NextResponse } from "next/server";
import { dashboardSnapshot } from "@/app/lib/dashboard-data";

export const dynamic = "force-static";

export async function GET() {
  return NextResponse.json({
    asOf: dashboardSnapshot.asOf,
    observedAt: dashboardSnapshot.observedAt,
    gate: dashboardSnapshot.gate,
    mode: dashboardSnapshot.mode,
    quality: { pass: 46, warn: 2, fail: 0 },
    domains: dashboardSnapshot.dataStatus,
  });
}
