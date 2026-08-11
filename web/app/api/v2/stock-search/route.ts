import {
  readPublishedArtifact,
  responseFromPublishedObject,
} from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export async function GET(request: Request) {
  const url = new URL(request.url);
  const requestedDate = url.searchParams.get("date") ?? undefined;
  if (requestedDate !== undefined && !ISO_DATE.test(requestedDate)) {
    return Response.json({ error: "invalid_date" }, { status: 400 });
  }
  const published = await readPublishedArtifact("stock_search", requestedDate);
  if (!published) {
    return Response.json(
      { error: requestedDate ? "stock_model_not_published_for_date" : "active_release_unavailable" },
      { status: requestedDate ? 404 : 503 },
    );
  }
  const response = responseFromPublishedObject(published, request);
  response.headers.set("X-Flow-Artifact", "stock_search");
  return response;
}
