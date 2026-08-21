import { getMarketHistoryRows } from "@/app/lib/dashboard-store";
import { getActiveReleaseManifest } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const asOf = url.searchParams.get("asOf") ?? undefined;
  if (asOf !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(asOf)) {
    return Response.json({ error: "invalid_date" }, { status: 400 });
  }
  const active = await getActiveReleaseManifest();
  if (!active) return Response.json({ error: "active_release_unavailable" }, { status: 503 });
  const target = asOf ?? active.asOf;
  if (target > active.asOf) {
    return Response.json({ error: "date_not_available", asOf: target }, { status: 404 });
  }
  const rows = await getMarketHistoryRows(target);
  const etag = `"${active.releaseId}:${target}"`;
  const headers = {
    "Cache-Control": "private, max-age=300, must-revalidate",
    ETag: etag,
    "X-Flow-As-Of": target,
    "X-Flow-Release-Id": active.releaseId,
  };
  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return Response.json(
    { asOf: target, releaseId: active.releaseId, rows },
    { headers },
  );
}
