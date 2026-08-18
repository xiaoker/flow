import {
  readPublishedArtifact,
  responseFromPublishedObject,
} from "@/app/lib/release-store";
import { flowBuildHeaders } from "@/app/lib/build-info";

export const dynamic = "force-dynamic";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHARD = /^stock_[0-7]$/;

export async function GET(
  request: Request,
  context: { params: Promise<{ shard: string }> },
) {
  const { shard } = await context.params;
  if (!SHARD.test(shard)) return Response.json({ error: "unknown_stock_shard" }, { status: 404, headers: flowBuildHeaders() });
  const url = new URL(request.url);
  const requestedDate = url.searchParams.get("date") ?? undefined;
  if (requestedDate !== undefined && !ISO_DATE.test(requestedDate)) {
    return Response.json({ error: "invalid_date" }, { status: 400, headers: flowBuildHeaders() });
  }
  const published = await readPublishedArtifact(shard, requestedDate);
  if (!published) {
    return Response.json(
      { error: requestedDate ? "stock_model_not_published_for_date" : "active_release_unavailable" },
      { status: requestedDate ? 404 : 503, headers: flowBuildHeaders() },
    );
  }
  const response = responseFromPublishedObject(published, request);
  response.headers.set("X-Flow-Artifact", shard);
  return response;
}
