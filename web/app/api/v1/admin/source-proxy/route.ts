import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const UPSTREAM_ORIGIN = "https://query.sse.com.cn";
const MAX_RESPONSE_BYTES = 10_000_000;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const COMPACT_DATE = /^\d{8}$/;
const ETF_CODE = /^\d{6}$/;
const ETF_CLASS = /^\d{2}$/;

const SSE_ETF_SCALE_SQL = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L";
const SSE_ETF_NAV_SQL = "COMMON_SSE_PL_ETFGGSGSHQD_L";
const SSE_ETF_PCF_BASIC_SQL = "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C";
const SSE_ETF_PCF_COMPONENT_SQL = "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_COMPONENT_C";

const COMMON_QUERY_PARAMETERS = new Set([
  "isPagination",
  "pageHelp.pageSize",
  "pageHelp.pageNo",
  "pageHelp.beginPage",
  "pageHelp.cacheSize",
  "pageHelp.endPage",
  "sqlId",
  "STAT_DATE",
  "FUNDID2",
  "ETF_VERSION",
  "ETF_CLASS",
  "FUND_CODE",
  "KEY_WORDS",
  "type",
]);
const MARGIN_QUERY_PARAMETERS = new Set([
  "isPagination",
  "beginDate",
  "endDate",
  "tabType",
  "stockCode",
  "pageHelp.pageSize",
  "pageHelp.pageNo",
  "pageHelp.beginPage",
  "pageHelp.cacheSize",
  "pageHelp.endPage",
]);

function secureEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function unauthorized() {
  return NextResponse.json({ error: "unauthorized" }, { status: 401 });
}

function invalidRequest(detail: string) {
  return NextResponse.json(
    { error: "invalid_source_request", detail },
    { status: 400 },
  );
}

function allowedParameters(path: string): Set<string> | null {
  if (path === "/commonQuery.do") return COMMON_QUERY_PARAMETERS;
  if (path === "/marketdata/tradedata/queryMargin.do") {
    return MARGIN_QUERY_PARAMETERS;
  }
  return null;
}

function validateQuery(path: string, query: URLSearchParams): string | null {
  const allowed = allowedParameters(path);
  if (!allowed) return "path is not allowlisted";
  for (const key of query.keys()) {
    if (key === "path") continue;
    if (!allowed.has(key)) return `query parameter is not allowlisted: ${key}`;
    if (query.getAll(key).length !== 1) return `duplicate query parameter: ${key}`;
  }
  if (path === "/commonQuery.do") {
    const sqlId = query.get("sqlId");
    if (
      ![
        SSE_ETF_SCALE_SQL,
        SSE_ETF_NAV_SQL,
        SSE_ETF_PCF_BASIC_SQL,
        SSE_ETF_PCF_COMPONENT_SQL,
      ].includes(sqlId ?? "")
    ) {
      return "sqlId is not allowlisted";
    }
    if (sqlId === SSE_ETF_SCALE_SQL) {
      if (!ISO_DATE.test(query.get("STAT_DATE") ?? "")) {
        return "STAT_DATE must be YYYY-MM-DD";
      }
    } else if (sqlId === SSE_ETF_NAV_SQL) {
      if (!ETF_CLASS.test(query.get("ETF_CLASS") ?? "")) {
        return "ETF_CLASS must be a two-digit code";
      }
      if (query.get("type") !== "inParams") {
        return "type must be inParams";
      }
      if (query.get("FUND_CODE") !== "" || query.get("KEY_WORDS") !== "") {
        return "FUND_CODE and KEY_WORDS must be empty";
      }
    } else {
      if (!ETF_CODE.test(query.get("FUNDID2") ?? "")) {
        return "FUNDID2 must be a six-digit ETF code";
      }
      if (query.get("isPagination") !== "false") {
        return "isPagination must be false";
      }
      if (
        sqlId === SSE_ETF_PCF_COMPONENT_SQL &&
        query.get("ETF_VERSION") !== "XML"
      ) {
        return "ETF_VERSION must be XML";
      }
    }
  } else {
    if (!COMPACT_DATE.test(query.get("beginDate") ?? "")) {
      return "beginDate must be YYYYMMDD";
    }
    if (!COMPACT_DATE.test(query.get("endDate") ?? "")) {
      return "endDate must be YYYYMMDD";
    }
  }
  return null;
}

export async function GET(request: Request) {
  const runtime = await runtimeEnv();
  const suppliedKey = request.headers
    .get("authorization")
    ?.replace(/^Bearer\s+/i, "");
  if (
    !runtime.INGEST_API_KEY ||
    !suppliedKey ||
    !secureEqual(suppliedKey, runtime.INGEST_API_KEY)
  ) {
    return unauthorized();
  }

  const requestUrl = new URL(request.url);
  const path = requestUrl.searchParams.get("path") ?? "";
  const validationError = validateQuery(path, requestUrl.searchParams);
  if (validationError) return invalidRequest(validationError);

  const upstreamUrl = new URL(path, UPSTREAM_ORIGIN);
  for (const [key, value] of requestUrl.searchParams) {
    if (key !== "path") upstreamUrl.searchParams.append(key, value);
  }
  const referer =
    path === "/commonQuery.do"
      ? "https://www.sse.com.cn/assortment/fund/etf/list/scale/"
      : "https://www.sse.com.cn/market/othersdata/margin/sum/";
  const upstream = await fetch(upstreamUrl, {
    headers: {
      Accept: "application/json,text/plain,*/*",
      Referer: referer,
      "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 " +
        "personal-trading-desk/0.1",
    },
    redirect: "follow",
  });
  if (upstream.url && new URL(upstream.url).hostname !== "query.sse.com.cn") {
    return NextResponse.json(
      { error: "unexpected_upstream_redirect" },
      { status: 502 },
    );
  }

  const declaredLength = Number(upstream.headers.get("content-length") ?? "0");
  if (declaredLength > MAX_RESPONSE_BYTES) {
    return NextResponse.json(
      { error: "upstream_response_too_large" },
      { status: 502 },
    );
  }
  if (!upstream.body) {
    return new Response(null, {
      status: upstream.status,
      headers: {
        "Cache-Control": "no-store",
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
        "X-Flow-Source-Transport": "cloudflare-sse-relay",
      },
    });
  }

  // Stream the allowlisted response through the Worker. The previous
  // arrayBuffer() call buffered the entire upstream payload in the request
  // isolate, which was unnecessary CPU/memory pressure during collection.
  // Keep a byte guard for sources that omit Content-Length.
  let streamedBytes = 0;
  const boundedBody = upstream.body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        streamedBytes += chunk.byteLength;
        if (streamedBytes > MAX_RESPONSE_BYTES) {
          controller.error(new Error("upstream_response_too_large"));
          return;
        }
        controller.enqueue(chunk);
      },
    }),
  );
  return new Response(boundedBody, {
    status: upstream.status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type":
        upstream.headers.get("Content-Type") ?? "application/json",
      "X-Flow-Source-Transport": "cloudflare-sse-relay",
    },
  });
}
