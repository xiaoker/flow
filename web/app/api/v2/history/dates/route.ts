import { getAvailableHistoryDates } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const dates = await getAvailableHistoryDates();
  return Response.json(
    {
      dates,
      count: dates.length,
      first: dates.at(0) ?? null,
      latest: dates.at(-1) ?? null,
    },
    { headers: { "Cache-Control": "private, max-age=300, must-revalidate" } },
  );
}
