/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  GITHUB_ACTIONS_TOKEN: string;
  RAW_BUCKET: R2Bucket;
  INGEST_API_KEY: string;
  FLOW_BUILD_SHA?: string;
  FLOW_DEPLOYED_AT?: string;
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
const DISPATCH_RETRY_DELAYS_MS = [0, 1_000, 3_000] as const;
const LATE_SCHEDULE_THRESHOLD_MS = 5 * 60 * 1_000;

function collectionWindowForCron(cron: string): "evening" | "morning" | "reconcile" {
  if (cron.startsWith("45 23")) return "morning";
  if (
    cron.startsWith("45 0") ||
    cron.startsWith("55 0") ||
    cron.startsWith("15 1") ||
    cron.startsWith("45 1")
  ) {
    return "reconcile";
  }
  return "evening";
}

function isRetryableDispatchStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

async function shouldDispatchReconcile(env: Env): Promise<boolean> {
  // The reconcile cron is a safety net, not a blind duplicate collector.  A
  // ready ledger means the target date has already reached the publish gate;
  // a future nextCheckAt means the source is still inside its declared
  // publication window.  If the ledger is unavailable we fail open so the
  // GitHub schedule remains an operational fallback rather than becoming a
  // single point of failure.
  if (!env.DB) return true;
  try {
    const target = await env.DB.prepare(
      `SELECT target_date AS targetDate
         FROM source_readiness
        GROUP BY target_date
        ORDER BY target_date DESC
        LIMIT 1`,
    ).first<{ targetDate: string }>();
    if (!target?.targetDate) return true;
    const result = await env.DB.prepare(
      `SELECT status, required, next_check_at AS nextCheckAt
         FROM source_readiness
        WHERE target_date = ?`,
    ).bind(target.targetDate).all<{ status: string; required: number; nextCheckAt: string | null }>();
    const rows = (result.results ?? []).filter((row) => Number(row.required) === 1);
    if (!rows.length) return true;
    if (rows.some((row) => row.status === "error")) return false;
    const now = Date.now();
    if (rows.some((row) => row.status === "pending" &&
      (!row.nextCheckAt || Number.isNaN(Date.parse(row.nextCheckAt)) || Date.parse(row.nextCheckAt) <= now))) {
      return true;
    }
    return false;
  } catch (error: unknown) {
    console.warn("Source readiness ledger unavailable; dispatching reconcile fail-open", {
      detail: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200),
    });
    return true;
  }
}

function withDiagnostics(response: Response, env: Env): Response {
  const next = new Response(response.body, response);
  if (env.FLOW_BUILD_SHA) next.headers.set("X-Flow-Build-Sha", env.FLOW_BUILD_SHA);
  if (env.FLOW_DEPLOYED_AT) next.headers.set("X-Flow-Deployed-At", env.FLOW_DEPLOYED_AT);
  return next;
}

async function dispatchDailyCollection(
  env: Env,
  controller: ScheduledController,
): Promise<void> {
  if (!env.GITHUB_ACTIONS_TOKEN) {
    throw new Error("GITHUB_ACTIONS_TOKEN is not configured");
  }

  const window = collectionWindowForCron(controller.cron);
  if (window === "reconcile" && !(await shouldDispatchReconcile(env))) {
    console.info("Skipped reconcile window because source readiness is not due", {
      cron: controller.cron,
    });
    return;
  }
  const scheduleLagMs = Math.max(0, Date.now() - controller.scheduledTime);
  if (scheduleLagMs > LATE_SCHEDULE_THRESHOLD_MS) {
    console.warn("Cloudflare Cron delivered a late collection window", {
      cron: controller.cron,
      window,
      scheduleLagMs,
      scheduledTime: new Date(controller.scheduledTime).toISOString(),
    });
  }
  let lastError: Error | undefined;

  for (let attempt = 0; attempt < DISPATCH_RETRY_DELAYS_MS.length; attempt += 1) {
    const delay = DISPATCH_RETRY_DELAYS_MS[attempt];
    if (delay > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, delay));
    }
    let response: Response;
    try {
      response = await fetch(DAILY_DATA_DISPATCH_URL, {
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
          inputs: { collection_window: window },
        }),
      });
    } catch (error: unknown) {
      lastError = error instanceof Error ? error : new Error(String(error));
      if (attempt === DISPATCH_RETRY_DELAYS_MS.length - 1) {
        throw lastError;
      }
      console.warn("GitHub workflow dispatch request failed; retrying", {
        cron: controller.cron,
        window,
        attempt: attempt + 1,
        scheduleLagMs,
        detail: lastError.message.slice(0, 500),
      });
      continue;
    }

    if (response.status === 200 || response.status === 204) {
      console.info("Dispatched Daily market data workflow from Cloudflare Cron", {
        cron: controller.cron,
        window,
        attempt: attempt + 1,
        scheduleLagMs,
      });
      return;
    }

    const detail = (await response.text()).slice(0, 500);
    lastError = new Error(
      `GitHub workflow dispatch failed with ${response.status}: ${detail}`,
    );
    if (!isRetryableDispatchStatus(response.status)) {
      throw lastError;
    }
    if (attempt === DISPATCH_RETRY_DELAYS_MS.length - 1) {
      throw lastError;
    }
    console.warn("GitHub workflow dispatch will be retried", {
      cron: controller.cron,
      window,
      attempt: attempt + 1,
      status: response.status,
      scheduleLagMs,
    });
  }

  throw lastError ?? new Error("GitHub workflow dispatch failed without a response");
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
      return withDiagnostics(await handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths), env);
    }

    return withDiagnostics(await handler.fetch(request, env, ctx), env);
  },
  async scheduled(
    controller: ScheduledController,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    ctx.waitUntil(
      dispatchDailyCollection(env, controller).catch((error: unknown) => {
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
