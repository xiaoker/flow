import {
  getActiveReleaseManifest,
  responseFromPublishedObject,
} from "@/app/lib/release-store";
import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const ALLOWED_VIEWS = new Set([
  "bootstrap",
  "overview",
  "radar",
  "rotation",
  "national",
  "connect",
  "margin",
]);

export async function GET(
  request: Request,
  context: { params: Promise<{ view: string }> },
) {
  const { view } = await context.params;
  if (!ALLOWED_VIEWS.has(view)) {
    return Response.json({ error: "unknown_view" }, { status: 404 });
  }
  const active = await getActiveReleaseManifest();
  const runtime = await runtimeEnv();
  if (!active || !runtime.RAW_BUCKET) {
    return Response.json({ error: "active_release_unavailable" }, { status: 503 });
  }
  const artifacts = active.manifest.artifacts;
  const artifact = artifacts && typeof artifacts === "object" && !Array.isArray(artifacts)
    ? (artifacts as Record<string, unknown>)[view]
    : null;
  const key = artifact && typeof artifact === "object" && !Array.isArray(artifact)
    ? (artifact as Record<string, unknown>).key
    : null;
  if (typeof key !== "string" || !key.startsWith("market-data/releases/")) {
    return Response.json({ error: "view_not_published" }, { status: 503 });
  }
  const object = await runtime.RAW_BUCKET.get(key);
  if (!object) return Response.json({ error: "view_not_found" }, { status: 503 });
  const response = responseFromPublishedObject(
    { object, asOf: active.asOf, releaseId: active.releaseId, historical: false },
    request,
  );
  response.headers.set("X-Flow-View", view);
  return response;
}
