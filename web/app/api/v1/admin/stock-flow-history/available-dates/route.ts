import { runtimeEnv } from "@/app/lib/dashboard-store";
import { getActiveReleaseManifest } from "@/app/lib/release-store";

export const dynamic = "force-dynamic";

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA = /^[a-f0-9]{64}$/;
const MAX_BYTES = 5_000_000;

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return difference === 0;
}

function invalid(): Response {
  return Response.json({ error: "invalid_active_pressure_evidence" }, { status: 503 });
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !supplied || !secureEqual(supplied, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.RAW_BUCKET || !runtime.DB) return Response.json({ error: "storage_unavailable" }, { status: 503 });
  try {
    const active = await getActiveReleaseManifest("etf_penetration");
    if (!active) return Response.json({ status: "no_active_release", availableDates: [] });
    const artifacts = active.manifest.artifacts;
    const pressure = artifacts && typeof artifacts === "object" && !Array.isArray(artifacts)
      ? (artifacts as Record<string, unknown>).pressure : null;
    if (!pressure || typeof pressure !== "object" || Array.isArray(pressure)) return invalid();
    const { key, sha256, bytes } = pressure as Record<string, unknown>;
    if (typeof key !== "string" || !key.startsWith(`market-data/releases/${active.asOf}/${active.releaseId}/`) ||
        typeof sha256 !== "string" || !SHA.test(sha256) ||
        typeof bytes !== "number" || !Number.isSafeInteger(bytes) || bytes < 1 || bytes > MAX_BYTES) return invalid();
    const object = await runtime.RAW_BUCKET.get(key);
    if (!object?.body || object.size !== bytes || object.customMetadata?.asOf !== active.asOf ||
        object.customMetadata?.releaseId !== active.releaseId) return invalid();
    const buffer = await object.arrayBuffer();
    if (buffer.byteLength !== bytes || buffer.byteLength > MAX_BYTES) return invalid();
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buffer)),
      (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (hash !== sha256) return invalid();
    const payload = JSON.parse(new TextDecoder().decode(buffer)) as Record<string, unknown>;
    const data = payload.data as Record<string, unknown> | undefined;
    const model = data?.pressure as Record<string, unknown> | undefined;
    const dates = model?.availableDates;
    if (payload.asOf !== active.asOf || model?.asOf !== active.asOf || !Array.isArray(dates) ||
        dates.length < 1 || dates.length > 21 ||
        dates.some((date) => typeof date !== "string" || !DATE.test(date) || date > active.asOf) ||
        dates.some((date, index) => index > 0 && date <= dates[index - 1])) return invalid();
    return Response.json({ status: "found", asOf: active.asOf, releaseId: active.releaseId, availableDates: dates },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    console.error("active pressure date evidence read failed", error);
    return invalid();
  }
}
