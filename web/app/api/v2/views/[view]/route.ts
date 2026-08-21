import {
  getReleaseManifestForDate,
  responseFromPublishedObject,
} from "@/app/lib/release-store";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import { flowBuildHeaders } from "@/app/lib/build-info";
import { READ_MODEL_SLOTS, type ReadModelSlot } from "@/app/lib/read-model-contract";

export const dynamic = "force-dynamic";

const ALLOWED_VIEWS = new Set([
  "bootstrap",
  "overview",
  "radar",
  "radar_flow",
  "rotation",
  "national",
  "connect",
  "margin",
  "pressure",
]);
const PENETRATION_VIEWS = new Set(["radar_flow", "pressure"]);

export async function GET(
  request: Request,
  context: { params: Promise<{ view: string }> },
) {
  const { view } = await context.params;
  if (!ALLOWED_VIEWS.has(view)) {
    return Response.json({ error: "unknown_view" }, { status: 404, headers: flowBuildHeaders() });
  }
  const requestedDate = new URL(request.url).searchParams.get("date") ?? undefined;
  if (requestedDate !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(requestedDate)) {
    return Response.json({ error: "invalid_date" }, { status: 400, headers: flowBuildHeaders() });
  }
  const runtime = await runtimeEnv();
  const requestedSlot = new URL(request.url).searchParams.get("slot");
  if (requestedSlot !== null && !READ_MODEL_SLOTS.includes(requestedSlot as ReadModelSlot)) {
    return Response.json({ error: "invalid_release_slot" }, { status: 400, headers: flowBuildHeaders() });
  }
  const slot = (requestedSlot ?? (runtime.PCF_CAPTURE_V2_READ_ENABLED === "1" && PENETRATION_VIEWS.has(view)
    ? "etf_penetration"
    : "dashboard")) as ReadModelSlot;
  if (slot === "etf_penetration" && runtime.PCF_CAPTURE_V2_READ_ENABLED !== "1") {
    return Response.json({ error: "penetration_slot_disabled" }, { status: 503, headers: flowBuildHeaders() });
  }
  const active = await getReleaseManifestForDate(requestedDate, slot);
  if (!active || !runtime.RAW_BUCKET) {
    return Response.json({ error: "active_release_unavailable" }, { status: 503, headers: flowBuildHeaders() });
  }
  const artifacts = active.manifest.artifacts;
  const artifact = artifacts && typeof artifacts === "object" && !Array.isArray(artifacts)
    ? (artifacts as Record<string, unknown>)[view]
    : null;
  const key = artifact && typeof artifact === "object" && !Array.isArray(artifact)
    ? (artifact as Record<string, unknown>).key
    : null;
  const releasePrefix = `market-data/releases/${active.asOf}/${active.releaseId}/`;
  if (typeof key !== "string" || !key.startsWith(releasePrefix)) {
    return Response.json({ error: "view_not_published" }, { status: 503, headers: flowBuildHeaders() });
  }
  const object = await runtime.RAW_BUCKET.get(key);
  if (
    !object ||
    object.customMetadata?.asOf !== active.asOf ||
    object.customMetadata?.releaseId !== active.releaseId
  ) return Response.json({ error: "view_not_found" }, { status: 503, headers: flowBuildHeaders() });
  const response = responseFromPublishedObject(
    { object, asOf: active.asOf, releaseId: active.releaseId, historical: requestedDate !== undefined && requestedDate !== active.asOf },
    request,
  );
  response.headers.set("X-Flow-View", view);
  response.headers.set("X-Flow-Release-Slot", slot);
  return response;
}
