import { getActiveReleaseManifest } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const active = await getActiveReleaseManifest();
  if (!active) {
    return Response.json(
      { error: "active_release_unavailable" },
      { status: 503, headers: { "Cache-Control": "private, max-age=30, must-revalidate" } },
    );
  }
  const etag = `"${active.manifestSha256}"`;
  const headers = {
    "Cache-Control": "private, max-age=30, must-revalidate",
    ETag: etag,
    "X-Flow-As-Of": active.asOf,
    "X-Flow-Release-Id": active.releaseId,
  };
  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers });
  }
  return Response.json(active, {
    headers,
  });
}
