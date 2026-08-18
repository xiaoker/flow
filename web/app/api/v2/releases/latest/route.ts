import { getActiveReleaseManifest } from "@/app/lib/release-store";
import { flowBuildHeaders, flowBuildInfo } from "@/app/lib/build-info";

export const dynamic = "force-dynamic";

const RELEASE_CACHE_CONTROL = "private, no-store, must-revalidate";

export async function GET(request: Request) {
  const active = await getActiveReleaseManifest();
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
  return Response.json({ ...active, ...flowBuildInfo() }, {
    headers,
  });
}
