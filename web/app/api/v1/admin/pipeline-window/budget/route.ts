import { runtimeEnv } from "@/app/lib/dashboard-store";
import { sharedBudgetRequest } from "@/app/lib/pipeline-shared-budget";

export const dynamic = "force-dynamic";
export async function GET(request: Request) { return sharedBudgetRequest(request, await runtimeEnv()); }
export async function POST(request: Request) { return sharedBudgetRequest(request, await runtimeEnv()); }
