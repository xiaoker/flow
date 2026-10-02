import { runtimeEnv } from "@/app/lib/dashboard-store";
import { checkpointObjectRequest } from "@/app/lib/pipeline-checkpoint-object";

export const dynamic = "force-dynamic";
export async function GET(request: Request) {
  return checkpointObjectRequest(request, await runtimeEnv());
}
export async function POST(request: Request) {
  return checkpointObjectRequest(request, await runtimeEnv());
}
