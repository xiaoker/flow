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
  let sourceReadinessReady = false;
  let bseMarginColumnReady = false;
  if (runtime.DB) {
    try {
      await runtime.DB.prepare("SELECT observed_at FROM release_manifest LIMIT 1").first();
      await runtime.DB.prepare("SELECT observed_at FROM active_release LIMIT 1").first();
      observedAtColumnsReady = true;
    } catch {
      observedAtColumnsReady = false;
    }
    try {
      // Keep this probe aligned with the deployed source_readiness schema.  A
      // capability check must only answer whether the ledger exists; querying
      // a legacy/non-existent column would silently hide the feature and make
      // every ETF-penetration publish fail closed.
      await runtime.DB.prepare("SELECT status, overall_status FROM source_readiness LIMIT 1").first();
      sourceReadinessReady = true;
    } catch {
      sourceReadinessReady = false;
    }
    try {
      await runtime.DB.prepare("SELECT bse_balance FROM market_history_daily LIMIT 1").first();
      bseMarginColumnReady = true;
    } catch {
      bseMarginColumnReady = false;
    }
  }
  const features = observedAtColumnsReady
    ? [
        "requiredArtifacts",
        "sameDateObservedAtOrder",
      "previousCompatibleRelease",
      "radarFlowView",
        "releaseSlots",
        "capabilityContractV07",
        ...(sourceReadinessReady ? ["sourceReadinessLedger"] : []),
        ...(bseMarginColumnReady ? ["bseMarginSeparateScope"] : []),
      ]
    : [];

  return Response.json(
    {
      contract: observedAtColumnsReady ? "read-model-v3" : "read-model-v2",
      readModelManifestSchemas: observedAtColumnsReady ? [2, 3, 4] : [2],
      schemaVersions: observedAtColumnsReady ? [2, 3, 4] : [2],
      pressureSchemaVersions: [2],
      stockShardCounts: [8],
      readModelSlots: ["dashboard", "etf_penetration"],
      maxArtifacts: 20,
      maxArtifactBytes: 5_000_000,
      features,
      deploymentCommit: runtime.FLOW_BUILD_SHA ?? "unknown",
      deployedAt: runtime.FLOW_DEPLOYED_AT ?? null,
      migration: {
        releaseObservedAtColumns: observedAtColumnsReady,
        sourceReadinessLedger: sourceReadinessReady,
        marketHistoryBseColumn: bseMarginColumnReady,
      },
      marketHistoryScopeVersion: bseMarginColumnReady ? "sse-szse-bse-v1" : "sse-szse-v0",
    },
    { headers: { "Cache-Control": "private, no-store" } },
  );
}
