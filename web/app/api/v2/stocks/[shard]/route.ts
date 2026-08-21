import {
  readPublishedArtifact,
  responseFromPublishedObject,
} from "@/app/lib/release-store";
import { flowBuildHeaders } from "@/app/lib/build-info";
import { runtimeEnv } from "@/app/lib/dashboard-store";

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
  const runtime = await runtimeEnv();
  const requestedSlot = url.searchParams.get("slot");
  const slot = requestedSlot ?? (runtime.PCF_CAPTURE_V2_READ_ENABLED === "1" ? "etf_penetration" : "dashboard");
  if (slot !== "dashboard" && slot !== "etf_penetration") {
    return Response.json({ error: "invalid_release_slot" }, { status: 400, headers: flowBuildHeaders() });
  }
  if (slot === "etf_penetration" && runtime.PCF_CAPTURE_V2_READ_ENABLED !== "1") {
    return Response.json({ error: "penetration_slot_disabled" }, { status: 503, headers: flowBuildHeaders() });
  }
  const published = await readPublishedArtifact(shard, requestedDate, slot);
  if (!published) {
    return Response.json(
      { error: requestedDate ? "stock_model_not_published_for_date" : "active_release_unavailable" },
      { status: requestedDate ? 404 : 503, headers: flowBuildHeaders() },
    );
  }
  const response = responseFromPublishedObject(published, request);
  response.headers.set("X-Flow-Artifact", shard);
  response.headers.set("X-Flow-Release-Slot", slot);
  return response;
}
