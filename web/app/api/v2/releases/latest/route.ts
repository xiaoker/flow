import { getActiveReleaseManifest } from "@/app/lib/release-store";
import { flowBuildHeaders, flowBuildInfo } from "@/app/lib/build-info";
import { runtimeEnv } from "@/app/lib/dashboard-store";
import { READ_MODEL_SLOTS, type ReadModelSlot } from "@/app/lib/read-model-contract";

export const dynamic = "force-dynamic";

const RELEASE_CACHE_CONTROL = "private, no-store, must-revalidate";

export async function GET(request: Request) {
  const requestedSlot = new URL(request.url).searchParams.get("slot") ?? "dashboard";
  if (!READ_MODEL_SLOTS.includes(requestedSlot as ReadModelSlot)) {
    return Response.json({ error: "invalid_release_slot" }, { status: 400, headers: flowBuildHeaders() });
  }
  const slot = requestedSlot as ReadModelSlot;
  if (slot === "etf_penetration" && (await runtimeEnv()).PCF_CAPTURE_V2_READ_ENABLED !== "1") {
    return Response.json({ error: "penetration_slot_disabled" }, { status: 503, headers: flowBuildHeaders() });
  }
  const active = await getActiveReleaseManifest(slot);
  if (!active) {
    return Response.json(
      { error: "active_release_unavailable" },
      { status: 503, headers: { "Cache-Control": RELEASE_CACHE_CONTROL, ...flowBuildHeaders() } },
    );
  }
  const etag = `"${active.manifestSha256}"`;
  const headers = {
    "Cache-Control": RELEASE_CACHE_CONTROL,
    ETag: etag,
    "X-Flow-Release-Id": active.releaseId,
    "X-Flow-As-Of": active.asOf,
    ...flowBuildHeaders(),
  };
  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return Response.json({ ...active, slot, ...flowBuildInfo() }, {
    headers,
  });
}
