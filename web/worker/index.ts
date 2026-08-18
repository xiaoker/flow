/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";
import { flowBuildHeaders } from "../app/lib/build-info";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  GITHUB_ACTIONS_TOKEN: string;
  RAW_BUCKET: R2Bucket;
  INGEST_API_KEY: string;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

interface ScheduledController {
  cron: string;
  scheduledTime: number;
}

const DAILY_DATA_DISPATCH_URL =
  "https://api.github.com/repos/xiaoker/flow/actions/workflows/daily-data.yml/dispatches";

function collectionWindowForCron(cron: string): "evening" | "morning" | "reconcile" {
  if (cron.startsWith("45 23")) return "morning";
  if (cron.startsWith("45 0") || cron.startsWith("55 0")) return "reconcile";
  return "evening";
}

async function dispatchDailyCollection(env: Env, cron: string): Promise<void> {
  if (!env.GITHUB_ACTIONS_TOKEN) {
    throw new Error("GITHUB_ACTIONS_TOKEN is not configured");
  }
  const response = await fetch(DAILY_DATA_DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_ACTIONS_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "flow-cloudflare-scheduler",
      "X-GitHub-Api-Version": "2026-03-10",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: { collection_window: collectionWindowForCron(cron) },
    }),
  });
  if (response.status !== 200 && response.status !== 204) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(
      `GitHub workflow dispatch failed with ${response.status}: ${detail}`,
    );
  }
  console.info("Dispatched Daily market data workflow from Cloudflare Cron");
}

// Image security config. SVG sources with .svg extension auto-skip the
// optimization endpoint on the client side (served directly, no proxy).
// To route SVGs through the optimizer (with security headers), set
// dangerouslyAllowSVG: true in next.config.js and uncomment below:
// const imageConfig: ImageConfig = { dangerouslyAllowSVG: true };

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // The app-router bundle runs in a separate Vite environment. Keep the
    // request's Cloudflare bindings visible to route handlers and local QA.
    (globalThis as typeof globalThis & { __FLOW_RUNTIME_ENV__?: Env }).__FLOW_RUNTIME_ENV__ = env;
    const url = new URL(request.url);

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    const response = await handler.fetch(request, env, ctx);
    const headers = new Headers(response.headers);
    for (const [name, value] of Object.entries(flowBuildHeaders())) headers.set(name, value);
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
  },
  async scheduled(
    controller: ScheduledController,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    ctx.waitUntil(
      dispatchDailyCollection(env, controller.cron).catch((error: unknown) => {
        const detail = error instanceof Error ? error.message : String(error);
        console.error("Daily collection dispatch failed", {
          cron: controller.cron,
          scheduledTime: new Date(controller.scheduledTime).toISOString(),
          detail,
        });
      }),
    );
  },
};

export default worker;
