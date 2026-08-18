import { getMarketHistoryRows } from "@/app/lib/dashboard-store";
import { flowBuildHeaders } from "@/app/lib/build-info";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const asOf = url.searchParams.get("asOf") ?? undefined;
  if (asOf !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(asOf)) {
    return Response.json({ error: "invalid_date" }, { status: 400 });
  }
  const rows = await getMarketHistoryRows(asOf);
  return Response.json(
    { asOf: asOf ?? null, rows },
    { headers: { "Cache-Control": "private, max-age=300, must-revalidate", ...flowBuildHeaders() } },
  );
}
