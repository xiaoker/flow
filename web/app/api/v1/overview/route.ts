import { NextResponse } from "next/server";
import { dashboardSnapshot } from "@/app/lib/dashboard-data";

export const dynamic = "force-static";

export async function GET() {
  return NextResponse.json(dashboardSnapshot, {
    headers: {
      "Cache-Control": "public, max-age=300, stale-while-revalidate=3600",
      "X-Data-Mode": "phase0-snapshot",
    },
  });
}
