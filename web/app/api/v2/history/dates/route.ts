import { getAvailableHistoryDates } from "@/app/lib/dashboard-store";
import { getActiveReleaseManifest } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const [dates, active] = await Promise.all([
    getAvailableHistoryDates(),
    getActiveReleaseManifest(),
  ]);
  const etag = active ? `"${active.releaseId}:${dates.length}"` : null;
  const headers: Record<string, string> = {
    "Cache-Control": "private, max-age=60, must-revalidate",
  };
  if (etag) {
    headers.ETag = etag;
    headers["X-Flow-As-Of"] = active?.asOf ?? "";
    headers["X-Flow-Release-Id"] = active?.releaseId ?? "";
  }
  if (etag && request.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return Response.json(
    {
      dates,
      count: dates.length,
      first: dates.at(0) ?? null,
      latest: dates.at(-1) ?? null,
    },
    { headers },
  );
}
