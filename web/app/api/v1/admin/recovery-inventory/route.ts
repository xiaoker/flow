import { runtimeEnv } from "@/app/lib/dashboard-store";
import {
  collectRecoveryInventory,
  isRecoveryInventoryAuthorized,
  parseInventoryQuery,
  recoveryInventoryHttpStatus,
} from "@/app/lib/recovery-inventory";

export const dynamic = "force-dynamic";

function response(body: Record<string, unknown>, status = 200): Response {
  return Response.json(body, {
    status,
    headers: { "Cache-Control": "private, no-store" },
  });
}

export async function GET(request: Request): Promise<Response> {
  const runtime = await runtimeEnv();
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!isRecoveryInventoryAuthorized(supplied, runtime.INGEST_API_KEY ?? "")) {
    return response({ error: "unauthorized" }, 401);
  }

  let query;
  try {
    query = parseInventoryQuery(new URL(request.url).searchParams);
  } catch (error) {
    return response({ error: error instanceof Error ? error.message : "invalid_inventory_query" }, 400);
  }

  const result = await collectRecoveryInventory(runtime, query);
  return response(result as unknown as Record<string, unknown>, recoveryInventoryHttpStatus(result));
}
