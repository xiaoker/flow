import { getActiveReleaseManifest } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

export async function GET() {
  const active = await getActiveReleaseManifest();
  if (!active) {
    return Response.json(
      { error: "active_release_unavailable" },
      { status: 503, headers: { "Cache-Control": "private, max-age=30, must-revalidate" } },
    );
  }
  return Response.json(active, {
    headers: { "Cache-Control": "private, max-age=30, must-revalidate" },
  });
}
