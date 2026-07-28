import { NextResponse } from "next/server";
import { runtimeEnv } from "@/app/lib/dashboard-store";

export const dynamic = "force-dynamic";

const UPSTREAM_ORIGIN = "https://query.sse.com.cn";
const MAX_RESPONSE_BYTES = 10_000_000;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const COMPACT_DATE = /^\d{8}$/;

const COMMON_QUERY_PARAMETERS = new Set([
  "isPagination",
  "pageHelp.pageSize",
  "pageHelp.pageNo",
  "pageHelp.beginPage",
  "pageHelp.cacheSize",
  "pageHelp.endPage",
  "sqlId",
  "STAT_DATE",
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
    if (
      query.get("sqlId") !==
      "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
    ) {
      return "sqlId is not allowlisted";
    }
    if (!ISO_DATE.test(query.get("STAT_DATE") ?? "")) {
      return "STAT_DATE must be YYYY-MM-DD";
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

  const content = await upstream.arrayBuffer();
  if (content.byteLength > MAX_RESPONSE_BYTES) {
    return NextResponse.json(
      { error: "upstream_response_too_large" },
      { status: 502 },
    );
  }
  return new Response(content, {
    status: upstream.status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type":
        upstream.headers.get("Content-Type") ?? "application/json",
      "X-Flow-Source-Transport": "cloudflare-sse-relay",
    },
  });
}
