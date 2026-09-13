/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";
import { flowBuildHeaders } from "../app/lib/build-info";
import {
  buildPipelineRunKey,
  claimPipelineWindow,
  getPipelineWindow,
  markPipelineWindow,
  PIPELINE_WINDOW_CONTRACT_VERSION,
  shanghaiDate,
  type PipelineWindowName,
} from "../app/lib/pipeline-window";
import {
  getMarketDayState,
  parseMarketDayStateRow,
  persistMarketDayProjection,
  projectMarketDay,
  shouldDispatchHeavyCollection,
  type SourceObservationSummary,
} from "../app/lib/market-day-projection";
import { probeMarketDaySources } from "../app/lib/market-day-probe";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  GITHUB_ACTIONS_TOKEN: string;
  RAW_BUCKET: R2Bucket;
  INGEST_API_KEY: string;
  PIPELINE_WINDOW_GATE_ENABLED?: string;
  /** v0.5 low-cost source probe; remains off until shadow validation is authorised. */
  MARKET_DAY_PROBE_ENABLED?: string;
  /** Recovery control-plane gate; intentionally unset/disabled in production during R1. */
  PIPELINE_RECOVERY_ENABLED?: string;
  PIPELINE_WINDOW_CONTRACT_VERSION?: string;
  HISTORY_IMPORT_ARCHIVE_ONLY?: string;
  PCF_CAPTURE_V2_READ_ENABLED?: string;
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

const MARKET_DATA_COLLECT_DISPATCH_URL =
  "https://api.github.com/repos/xiaoker/flow/actions/workflows/market-data-collect.yml/dispatches";
const DISPATCH_RETRY_DELAYS_MS = [0, 1_000, 3_000] as const;
const LATE_SCHEDULE_THRESHOLD_MS = 5 * 60 * 1_000;
const MARKET_DAY_OBSERVATION_LIMIT = 64;

export function collectionWindowForCron(cron: string): PipelineWindowName {
  if (cron.startsWith("45 23")) return "morning";
  if (cron.startsWith("45 0") || cron.startsWith("55 0") || cron.startsWith("15 1")) return "reconcile";
  return "evening";
}

function isRetryableDispatchStatus(status: number): boolean {
  return status === 408 || status === 429 || status >= 500;
}

function isEnabledFlag(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

async function readMarketDayObservations(
  db: D1Database,
  targetDate: string,
): Promise<SourceObservationSummary[]> {
  const result = await db.prepare(`
    SELECT observation_id, source_id, dataset, target_date,
           source_business_date, published_date, status,
           source_fingerprint, observed_at
      FROM source_observation
     WHERE target_date = ?
     ORDER BY observed_at DESC, source_id ASC, dataset ASC
     LIMIT ?
  `).bind(targetDate, MARKET_DAY_OBSERVATION_LIMIT).all<SourceObservationSummary>();
  return (result.results ?? []).map((row) => ({
    observationId: String(row.observationId ?? (row as unknown as Record<string, unknown>).observation_id ?? ""),
    sourceId: String(row.sourceId ?? (row as unknown as Record<string, unknown>).source_id ?? ""),
    dataset: String(row.dataset ?? ""),
    targetDate: String(row.targetDate ?? (row as unknown as Record<string, unknown>).target_date ?? ""),
    sourceBusinessDate: (row.sourceBusinessDate ?? (row as unknown as Record<string, unknown>).source_business_date ?? null) as string | null,
    publishedDate: (row.publishedDate ?? (row as unknown as Record<string, unknown>).published_date ?? null) as string | null,
    status: String(row.status ?? ""),
    sourceFingerprint: String(row.sourceFingerprint ?? (row as unknown as Record<string, unknown>).source_fingerprint ?? ""),
    observedAt: String(row.observedAt ?? (row as unknown as Record<string, unknown>).observed_at ?? ""),
  }));
}

async function coordinateMarketDay(
  env: Env,
  targetDate: string,
  runKey: string,
): Promise<{ shouldDispatch: boolean; probeFingerprint: string }> {
  if (!env.DB) throw new Error("MARKET_DAY_PROBE_ENABLED requires DB");
  const observedAt = new Date().toISOString();
  // Probes are intentionally sequential and bounded.  They only retain
  // response metadata/prefix hashes; the heavy collector remains the owner of
  // raw bytes and full parsing.
  const probes = await probeMarketDaySources(targetDate, observedAt);
  const observations = await readMarketDayObservations(env.DB, targetDate);
  const previousRow = await getMarketDayState(env.DB, targetDate);
  const previous = previousRow ? parseMarketDayStateRow(previousRow) : null;
  const projection = await projectMarketDay({
    targetDate,
    observations,
    probes,
    projectedAt: observedAt,
  });
  const pipeline = await getPipelineWindow(env.DB, runKey);
  const shouldDispatch = shouldDispatchHeavyCollection(
    previous,
    projection,
    pipeline?.status ?? null,
  );
  const persisted = await persistMarketDayProjection(env.DB, projection);
  console.info("Market-day probe projection recorded", {
    targetDate,
    runKey,
    state: projection.state,
    nextAction: projection.nextAction,
    shouldDispatch,
    probeFingerprint: projection.probeFingerprint,
    sourceFingerprint: projection.sourceFingerprint,
    d1RowsWritten: persisted.rowsWritten,
  });
  return { shouldDispatch, probeFingerprint: projection.probeFingerprint };
}

async function dispatchDailyCollection(
  env: Env,
  controller: ScheduledController,
): Promise<void> {
  if (!env.GITHUB_ACTIONS_TOKEN) {
    throw new Error("GITHUB_ACTIONS_TOKEN is not configured");
  }

  const window = collectionWindowForCron(controller.cron);
  const windowDate = shanghaiDate(controller.scheduledTime);
  const contractVersion = env.PIPELINE_WINDOW_CONTRACT_VERSION ?? PIPELINE_WINDOW_CONTRACT_VERSION;
  const runKey = buildPipelineRunKey(windowDate, window, contractVersion);
  const leaseOwner = `cloudflare:${window}:${controller.scheduledTime}`;
  const gateEnabled = env.PIPELINE_WINDOW_GATE_ENABLED === "1" || env.PIPELINE_WINDOW_GATE_ENABLED === "true";
  let probeFingerprint: string | null = null;
  if (isEnabledFlag(env.MARKET_DAY_PROBE_ENABLED)) {
    if (!gateEnabled) throw new Error("MARKET_DAY_PROBE_ENABLED requires PIPELINE_WINDOW_GATE_ENABLED");
    const coordination = await coordinateMarketDay(env, windowDate, runKey);
    probeFingerprint = coordination.probeFingerprint;
    if (!coordination.shouldDispatch) {
      console.info("Market-day probe found no new source facts; skipping heavy collection", {
        cron: controller.cron,
        runKey,
        targetDate: windowDate,
        probeFingerprint,
      });
      return;
    }
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
  let leaseAcquired = false;
  if (gateEnabled) {
    if (!env.DB) throw new Error("PIPELINE_WINDOW_GATE_ENABLED requires DB");
    const claim = await claimPipelineWindow(env.DB, {
      runKey,
      windowDate,
      windowName: window,
      contractVersion,
      // The workflow resolves the exact trading date after checkout.  This is
      // the Shanghai business date of the scheduling window, not fabricated
      // market data.
      targetDate: windowDate,
      leaseOwner,
      sourceFingerprint: probeFingerprint,
    });
    if (claim.decision !== "acquired" && claim.decision !== "takeover") {
      console.info("Skipped duplicate daily collection window", {
        cron: controller.cron,
        runKey,
        decision: claim.decision,
        status: claim.status,
      });
      return;
    }
    leaseAcquired = true;
  }
  let lastError: Error | undefined;
  try {
    for (let attempt = 0; attempt < DISPATCH_RETRY_DELAYS_MS.length; attempt += 1) {
      const delay = DISPATCH_RETRY_DELAYS_MS[attempt];
      if (delay > 0) {
        await new Promise<void>((resolve) => setTimeout(resolve, delay));
      }
      let response: Response;
      try {
        response = await fetch(MARKET_DATA_COLLECT_DISPATCH_URL, {
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
            inputs: {
              collection_window: window,
              run_key: runKey,
              window_name: window,
              target_date: windowDate,
              lease_owner: leaseOwner,
              contract_version: contractVersion,
              delegate_publish: "true",
            },
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
          runKey,
          attempt: attempt + 1,
          scheduleLagMs,
          detail: lastError.message.slice(0, 500),
        });
        continue;
      }

      if (response.status === 200 || response.status === 204) {
        console.info("Dispatched market-data-collect workflow from Cloudflare Cron", {
          cron: controller.cron,
          window,
          runKey,
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
        runKey,
        attempt: attempt + 1,
        status: response.status,
        scheduleLagMs,
      });
    }

    throw lastError ?? new Error("GitHub workflow dispatch failed without a response");
  } catch (error: unknown) {
    if (leaseAcquired) {
      try {
        await markPipelineWindow(env.DB!, {
          runKey,
          leaseOwner,
          status: "FAILED_RETRYABLE",
          errorCode: "github_dispatch_failed",
        });
      } catch (markError: unknown) {
        console.error("Failed to mark pipeline window retryable", {
          runKey,
          detail: markError instanceof Error ? markError.message : String(markError),
        });
      }
    }
    throw error;
  }
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
    // Route handlers are built by Vite and carry the commit-specific build
    // identity.  The outer Worker entry is bundled by Wrangler separately and
    // may only have the local fallback available.  Never let that fallback
    // overwrite a verified identity from the app bundle, or advertise
    // `local` as a production identity on a route that has no build header.
    for (const [name, value] of Object.entries(flowBuildHeaders())) {
      const existing = headers.get(name);
      if (!existing && value !== "local") headers.set(name, value);
    }
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
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
