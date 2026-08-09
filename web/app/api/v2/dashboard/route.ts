import { getPublishedObject, responseFromPublishedObject } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const requestedDate = url.searchParams.get("date") ?? undefined;
  if (requestedDate !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(requestedDate)) {
    return Response.json({ error: "invalid_date" }, { status: 400 });
  }
  const published = await getPublishedObject(requestedDate);
  if (!published) {
    return Response.json(
      { error: requestedDate ? "date_not_available" : "published_snapshot_unavailable" },
      { status: requestedDate ? 404 : 503 },
    );
  }
  return responseFromPublishedObject(published, request);
}
