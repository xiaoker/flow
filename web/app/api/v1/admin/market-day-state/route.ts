import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  getMarketDayState,
  parseMarketDayStateRow,
} from "@/app/lib/market-day-projection";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isCalendarDate(value: string): boolean {
  if (!ISO_DATE.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.DB) return Response.json({ error: "database_unavailable" }, { status: 503 });

  const targetDate = new URL(request.url).searchParams.get("date") ?? "";
  if (!isCalendarDate(targetDate)) return Response.json({ error: "invalid_date" }, { status: 400 });
  try {
    const row = await getMarketDayState(runtime.DB, targetDate);
    if (!row) return Response.json({ error: "market_day_state_not_found", targetDate }, { status: 404 });
    return Response.json(parseMarketDayStateRow(row), {
      headers: { "Cache-Control": "private, no-store" },
    });
  } catch (error: unknown) {
    console.error("market-day state read failed", {
      detail: error instanceof Error ? error.message : "unknown_market_day_state_error",
    });
    return Response.json({ error: "market_day_state_unavailable" }, { status: 503 });
  }
}
