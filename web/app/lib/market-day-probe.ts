import { canonicalJson, sha256Hex } from "./source-observation.ts";
import {
  MARKET_DAY_SOURCE_DEFINITIONS,
  type MarketDayProbe,
  type MarketDaySourceKey,
} from "./market-day-projection.ts";

/**
 * Low-cost exchange probes used by the v0.5 shadow coordinator.
 *
 * The URLs and date rules mirror the already deployed official adapters.  A
 * probe is deliberately not a collector: it requests at most one small page
 * per required source, keeps only a bounded prefix, and never writes raw
 * responses.  The full collector remains responsible for parsing, storing and
 * validating official facts.
 */
export const MARKET_DAY_PROBE_VERSION = "flow-market-day-probe-v1" as const;
export const MARKET_DAY_PROBE_MAX_BYTES = 64 * 1024;
export const MARKET_DAY_PROBE_TIMEOUT_MS = 8_000;

type ProbeSpec = {
  sourceKey: Extract<MarketDaySourceKey, "sse_etf" | "szse_etf" | "sse_margin" | "szse_margin" | "bse_margin" | "stock_connect_summary">;
  url: (targetDate: string) => string;
  referer: string;
  dateBasis: string;
};

const PROBE_SPECS: readonly ProbeSpec[] = [
  {
    sourceKey: "sse_etf",
    url: (targetDate) => {
      const url = new URL("https://query.sse.com.cn/commonQuery.do");
      url.search = new URLSearchParams({
        isPagination: "true",
        "pageHelp.pageSize": "1",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        sqlId: "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        STAT_DATE: targetDate,
      }).toString();
      return url.toString();
    },
    referer: "https://www.sse.com.cn/assortment/fund/etf/list/scale/",
    dateBasis: "sse_official_trade_date",
  },
  {
    sourceKey: "szse_etf",
    url: () => {
      const url = new URL("https://fund.szse.cn/api/report/ShowReport/data");
      url.search = new URLSearchParams({
        SHOWTYPE: "JSON",
        CATALOGID: "1000_lf",
        TABKEY: "tab1",
        tab1PAGENO: "1",
        tab1PAGESIZE: "1",
      }).toString();
      return url.toString();
    },
    referer: "https://fund.szse.cn/marketdata/fundslist/index.html",
    dateBasis: "source_page_previous_trading_day",
  },
  {
    sourceKey: "sse_margin",
    url: (targetDate) => {
      const url = new URL("https://query.sse.com.cn/marketdata/tradedata/queryMargin.do");
      url.search = new URLSearchParams({
        isPagination: "true",
        beginDate: targetDate.replaceAll("-", ""),
        endDate: targetDate.replaceAll("-", ""),
        tabType: "",
        stockCode: "",
        "pageHelp.pageSize": "1",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
      }).toString();
      return url.toString();
    },
    referer: "https://www.sse.com.cn/market/othersdata/margin/sum/",
    dateBasis: "sse_margin_trade_date",
  },
  {
    sourceKey: "szse_margin",
    url: (targetDate) => {
      const url = new URL("https://www.szse.cn/api/report/ShowReport/data");
      url.search = new URLSearchParams({
        SHOWTYPE: "JSON",
        CATALOGID: "1837_xxpl",
        txtDate: targetDate,
        tab1PAGENO: "1",
        tab1PAGESIZE: "1",
      }).toString();
      return url.toString();
    },
    referer: "https://www.szse.cn/disclosure/margin/margin/index.html",
    dateBasis: "szse_margin_trade_date",
  },
  {
    sourceKey: "bse_margin",
    url: (targetDate) => {
      const url = new URL("https://www.bseinfo.net/rzrqjyyexxController/summaryInfoResult.do");
      url.search = new URLSearchParams({ callback: "cb", transDate: targetDate, page: "0" }).toString();
      return url.toString();
    },
    referer: "https://www.bseinfo.net/disclosure/rzrq_trans_list.html",
    dateBasis: "bse_official_summary_trade_date",
  },
  {
    sourceKey: "stock_connect_summary",
    url: (targetDate) => {
      const compact = targetDate.replaceAll("-", "");
      return `https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_${compact}c.js`;
    },
    referer: "https://www.hkex.com.hk/Mutual-Market/Stock-Connect/Statistics/Historical-Daily?sc_lang=zh-CN",
    dateBasis: "hkex_exact_daily_file",
  },
] as const;

function isDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function normalizeDate(value: string): string | null {
  const compact = value.replaceAll("/", "-");
  if (/^20\d{2}-\d{2}-\d{2}$/.test(compact)) return isDate(compact) ? compact : null;
  if (/^20\d{6}$/.test(value)) {
    const candidate = `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
    return isDate(candidate) ? candidate : null;
  }
  return null;
}

function dateMarkers(text: string): string[] {
  const matches = text.match(/20\d{2}[-/]\d{2}[-/]\d{2}|20\d{6}/g) ?? [];
  return [...new Set(matches.map(normalizeDate).filter((value): value is string => Boolean(value)))];
}

async function readBoundedBody(response: Response, maxBytes: number): Promise<{ bytes: Uint8Array; truncated: boolean }> {
  if (!response.body) return { bytes: new Uint8Array(), truncated: false };
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let truncated = false;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      const value = result.value;
      const remaining = maxBytes - total;
      if (remaining <= 0) {
        truncated = true;
        await reader.cancel();
        break;
      }
      if (value.byteLength > remaining) {
        chunks.push(value.slice(0, remaining));
        total += remaining;
        truncated = true;
        await reader.cancel();
        break;
      }
      chunks.push(value);
      total += value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { bytes, truncated };
}

function statusForResponse(status: number): MarketDayProbe["status"] {
  if (status === 404) return "NOT_PUBLISHED";
  if (status < 200 || status >= 400) return "SOURCE_UNAVAILABLE";
  return "PUBLISHED";
}

function markerForSpec(spec: ProbeSpec, targetDate: string, markers: string[]): {
  sourceBusinessDate: string | null;
  publishedDate: string | null;
} {
  if (spec.sourceKey === "stock_connect_summary" || spec.sourceKey === "bse_margin") {
    return { sourceBusinessDate: markers.includes(targetDate) ? targetDate : null, publishedDate: null };
  }
  if (spec.sourceKey === "szse_etf") {
    // SZSE's list page date is a publication watermark.  The exact preceding
    // trading date is resolved by the full collector using its official
    // trading-date calendar; a probe must not guess it.
    return { sourceBusinessDate: null, publishedDate: markers[0] ?? null };
  }
  return {
    sourceBusinessDate: markers.includes(targetDate) ? targetDate : null,
    publishedDate: markers[0] ?? null,
  };
}

async function probeOne(
  spec: ProbeSpec,
  targetDate: string,
  observedAt: string,
  fetchImpl: typeof fetch,
): Promise<MarketDayProbe> {
  const url = spec.url(targetDate);
  const headers = {
    Accept: "application/json,text/javascript,text/plain,*/*",
    Referer: spec.referer,
    "User-Agent": "personal-trading-desk/0.1 market-day-probe",
    Range: `bytes=0-${MARKET_DAY_PROBE_MAX_BYTES - 1}`,
  };
  let response: Response;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), MARKET_DAY_PROBE_TIMEOUT_MS);
    try {
      response = await fetchImpl(url, { method: "GET", headers, redirect: "follow", signal: controller.signal });
    } finally {
      clearTimeout(timeout);
    }
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.name : "probe_request_failed";
    const fingerprint = await sha256Hex(new TextEncoder().encode(canonicalJson({
      version: MARKET_DAY_PROBE_VERSION,
      sourceKey: spec.sourceKey,
      targetDate,
      status: "SOURCE_UNAVAILABLE",
      detail,
    })));
    return {
      sourceKey: spec.sourceKey,
      sourceId: MARKET_DAY_SOURCE_DEFINITIONS.find((item) => item.key === spec.sourceKey)!.sourceId,
      dataset: MARKET_DAY_SOURCE_DEFINITIONS.find((item) => item.key === spec.sourceKey)!.dataset,
      targetDate,
      status: "SOURCE_UNAVAILABLE",
      httpStatus: null,
      observedAt,
      sourceBusinessDate: null,
      publishedDate: null,
      fingerprint,
      reason: `probe_request_failed:${detail}`,
      evidence: { url, dateBasis: spec.dateBasis, errorType: detail },
    };
  }

  const body = await readBoundedBody(response, MARKET_DAY_PROBE_MAX_BYTES);
  const text = new TextDecoder().decode(body.bytes);
  const markers = dateMarkers(text);
  const dates = markerForSpec(spec, targetDate, markers);
  const status = statusForResponse(response.status);
  const source = MARKET_DAY_SOURCE_DEFINITIONS.find((item) => item.key === spec.sourceKey)!;
  const contentLength = response.headers.get("content-length");
  const fingerprint = await sha256Hex(new TextEncoder().encode(canonicalJson({
    version: MARKET_DAY_PROBE_VERSION,
    sourceKey: spec.sourceKey,
    targetDate,
    status,
    httpStatus: response.status,
    sourceBusinessDate: dates.sourceBusinessDate,
    publishedDate: dates.publishedDate,
    etag: response.headers.get("etag"),
    lastModified: response.headers.get("last-modified"),
    contentLength,
    bodySha256: await sha256Hex(body.bytes),
    truncated: body.truncated,
  })));
  const reason = status === "NOT_PUBLISHED"
    ? "source_not_published"
    : status === "SOURCE_UNAVAILABLE"
      ? "source_http_error"
      : dates.sourceBusinessDate || dates.publishedDate
        ? "probe_business_date_observed"
        : "probe_business_date_unknown";
  return {
    sourceKey: spec.sourceKey,
    sourceId: source.sourceId,
    dataset: source.dataset,
    targetDate,
    status,
    httpStatus: response.status,
    observedAt,
    sourceBusinessDate: dates.sourceBusinessDate,
    publishedDate: dates.publishedDate,
    fingerprint,
    reason,
    evidence: {
      url,
      dateBasis: spec.dateBasis,
      markers,
      contentType: response.headers.get("content-type"),
      contentLength,
      truncated: body.truncated,
    },
  };
}

/** Probe the six required official sources sequentially to avoid bursts. */
export async function probeMarketDaySources(
  targetDate: string,
  observedAt = new Date().toISOString(),
  fetchImpl: typeof fetch = fetch,
): Promise<MarketDayProbe[]> {
  if (!isDate(targetDate)) throw new Error("market_day_invalid_target_date");
  const probes: MarketDayProbe[] = [];
  for (const spec of PROBE_SPECS) {
    probes.push(await probeOne(spec, targetDate, observedAt, fetchImpl));
  }
  return probes;
}

export function probeSpecsForTest(): readonly ProbeSpec[] {
  return PROBE_SPECS;
}
