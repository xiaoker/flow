import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

export async function GET(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!runtime.INGEST_API_KEY || !suppliedKey || !secureEqual(suppliedKey, runtime.INGEST_API_KEY)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  let observedAtColumnsReady = false;
  if (runtime.DB) {
    try {
      await runtime.DB.prepare("SELECT observed_at FROM release_manifest LIMIT 1").first();
      await runtime.DB.prepare("SELECT observed_at FROM active_release LIMIT 1").first();
      observedAtColumnsReady = true;
    } catch {
      observedAtColumnsReady = false;
    }
  }

  return Response.json(
    {
      contract: observedAtColumnsReady ? "read-model-v3" : "read-model-v2",
      readModelManifestSchemas: observedAtColumnsReady ? [2, 3] : [2],
      schemaVersions: observedAtColumnsReady ? [2, 3] : [2],
      pressureSchemaVersions: [2],
      stockShardCounts: [8],
      maxArtifacts: 20,
      maxArtifactBytes: 5_000_000,
      features: observedAtColumnsReady
        ? ["requiredArtifacts", "sameDateObservedAtOrder", "previousCompatibleRelease", "radarFlowView"]
        : [],
      deploymentCommit: "unknown",
      migration: {
        releaseObservedAtColumns: observedAtColumnsReady,
      },
    },
    { headers: { "Cache-Control": "private, no-store" } },
  );
}
