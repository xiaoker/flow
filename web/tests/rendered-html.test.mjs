import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { isPublishableSnapshot } from "../app/lib/snapshot-policy.mjs";

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker;
}

const env = {
  ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
};
const context = { waitUntil() {}, passThroughOnException() {} };

test("server-renders the trading flow dashboard", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    env,
    context,
  );
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>[^<]*市场数据台[^<]*<\/title>/i);
  assert.match(html, /啸傲的市场资金流向交易台/);
  assert.match(html, /板块份额变化/);
  assert.match(html, /ETF 雷达/);
  assert.match(html, /南向净买入/);
  assert.match(html, /ETF 份额变化/);
  assert.match(html, /金额估算（亿元）/);
  assert.match(html, /官方事实/);
  assert.match(html, /公式计算/);
  assert.match(html, /日期核验/);
  assert.match(html, /未披露的数据不展示、不补造/);
  assert.match(html, /当前官方不披露北向净流入/);
  assert.match(html, /份额变化不等同资金金额/);
  assert.doesNotMatch(html, /板块资金轮动|资金流全景/);
  assert.doesNotMatch(html, /β\s*压强/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps the margin history preview inside its dashboard card", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /\.compact-panel\s*\{[^}]*overflow:\s*hidden/);
  assert.match(css, /\.margin-spark\s*\{[^}]*grid-template-columns:\s*repeat\(12,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.margin-spark\s*\{[^}]*padding:\s*0 16px 0 2px/);
  assert.match(css, /\.margin-spark-point\.is-latest\s*\{[^}]*text-align:\s*right/);
  assert.match(css, /\.margin-spark-point\.is-expansion\s*>\s*\.margin-spark-bar\s*\{[^}]*background:\s*#d95e61/);
  assert.match(css, /\.margin-spark-point\.is-contraction\s*>\s*\.margin-spark-bar\s*\{[^}]*background:\s*#55b887/);
  assert.match(css, /\.margin-spark-point\.is-latest\s*>\s*\.margin-spark-bar\s*\{[^}]*outline:\s*2px solid var\(--cyan\)/);
});

test("keeps ETF detail evidence and unavailable periods explicit", async () => {
  const [source, css] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);
  assert.match(source, /drawer-proof-grid/);
  assert.match(source, /展示快照日/);
  assert.match(source, /原始来源/);
  assert.match(source, /selectedShareHistory\.length >= 2/);
  assert.match(source, /复算锚点，不是原始逐日快照/);
  assert.match(source, /drawer-periods-more/);
  assert.match(css, /\.drawer-proof-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3/);
  assert.match(css, /\.drawer-chart-empty\s*\{[^}]*flex-direction:\s*column/);
  assert.match(css, /\.drawer-periods-more\s*\{/);
});

test("refreshes the latest dashboard and schedules collection through source release windows", async () => {
  const [source, configText] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"),
  ]);
  const config = JSON.parse(configText);
  assert.match(source, /AUTO_REFRESH_MS = 5 \* 60 \* 1000/);
  assert.match(source, /window\.setInterval\(refresh, AUTO_REFRESH_MS\)/);
  assert.match(source, /if \(!autoRefresh \|\| requestedDate\) return/);
  assert.deepEqual(config.triggers.crons, [
    "30 10 * * mon-fri",
    "45 23 * * sun-thu",
    "10 0 * * mon-fri",
  ]);
});

test("replaces repetitive margin day cards with derived diagnostics", async () => {
  const source = await readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8");
  assert.match(source, /最新完整数据日/);
  assert.match(source, /短期杠杆趋势/);
  assert.match(source, /融资买入活跃度/);
  assert.match(source, /沪深余额增量/);
  assert.match(source, /融券余额变化/);
  assert.doesNotMatch(source, /margin-history compact-history/);
});

test("places disclosure-separated connect channels in a dedicated decision strip", async () => {
  const [source, css, snapshotBuilder] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/trading_desk/dashboard.py", import.meta.url), "utf8"),
  ]);
  assert.match(source, /className="connect-top-summary"/);
  assert.match(source, /className="connect-heading-note"/);
  assert.match(source, /aria-label="南北互通当日摘要"/);
  assert.match(source, /connect-channel-south/);
  assert.match(source, /connect-channel-north/);
  assert.match(source, /南向资金/);
  assert.match(source, /北向活跃度/);
  assert.match(source, /仅成交额，不代表流入方向/);
  assert.match(css, /\.connect-top-summary\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.connect-top-summary\s*\{[^}]*margin-top:\s*16px/);
  assert.match(css, /@media \(max-width: 680px\)[\s\S]*\.connect-top-summary\s*\{\s*grid-template-columns:\s*1fr/);
  assert.match(source, /const displayMode = snapshot\.mode\.replace/);
  assert.match(snapshotBuilder, /"mode": "官方数据快照"/);
  assert.doesNotMatch(snapshotBuilder, /GitHub Actions/);
});

test("defines the national-team ETF watchlist without fabricating holder disclosures", async () => {
  const source = await readFile(
    new URL("../app/national-team-view.tsx", import.meta.url),
    "utf8",
  );
  const codes = [
    "510300",
    "510310",
    "510330",
    "510500",
    "159915",
    "159919",
    "512100",
    "510050",
    "588080",
    "510180",
    "159845",
    "512500",
  ];
  for (const code of codes) assert.match(source, new RegExp(`"${code}"`));
  const fundShortNames = [
    "沪深300ETF华泰柏瑞",
    "沪深300ETF易方达",
    "沪深300ETF华夏",
    "中证500ETF南方",
    "创业板ETF易方达",
    "沪深300ETF嘉实",
    "中证1000ETF南方",
    "上证50ETF华夏",
    "科创50ETF易方达",
    "上证180ETF华安",
    "中证1000ETF华夏",
    "中证500ETF华夏",
  ];
  for (const name of fundShortNames) assert.match(source, new RegExp(name));
  assert.match(source, /nationalTeamEtfs/);
  assert.match(source, /<th>基金简称<\/th>/);
  assert.doesNotMatch(source, /referenceName/);
  assert.match(source, /fundShortName/);
  assert.match(source, /正式持有人披露尚未接入/);
  assert.match(source, /不使用截图数字或第三方估算/);
  assert.match(source, /它不等同于国家队增减持/);
  assert.match(source, /type:\s*"line"/);
  assert.match(source, /历史节点＝当前官方总份额－对应周期份额变化/);
});

test("gives every ETF Radar detail view an official-share line chart", async () => {
  const source = await readFile(
    new URL("../app/dashboard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /drawer-share-chart/);
  assert.match(source, /selectedShareHistory/);
  assert.match(source, /name:\s*"官方总份额（亿份）"/);
  assert.match(source, /selectedShareHistory\.length >= 2/);
  assert.match(source, /type:\s*"line"/);
  assert.match(source, /不是现金净流入/);
});

test("requires explicit confirmation before opening a historical date", async () => {
  const source = await readFile(
    new URL("../app/dashboard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /setDraftDate\(event\.target\.value\)/);
  assert.match(source, />\s*查看\s*<\/button>/);
  assert.doesNotMatch(source, /onChange=\{\(event\) => event\.target\.value && openDate/);
});

test("exposes connect trend series without inventing northbound direction", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/connect/flows"),
    env,
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.northboundDisclosure, "turnover_only");
  assert.equal(payload.coverage.points, 3);
  assert.equal(payload.series.length, 3);
  assert.equal(payload.series[2].southboundNet, 109.97);
  assert.equal("northboundNet" in payload.series[2], false);
  assert.equal(payload.evidence.southbound.kind, "calculated");
  assert.equal(payload.evidence.northbound.kind, "official");
  assert.equal(payload.evidence.northbound.directionAvailable, false);
});

test("exposes the machine-readable data status endpoint", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/data-status"),
    env,
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.gate, "conditional_pass");
  assert.deepEqual(payload.quality, { pass: 46, warn: 2, fail: 0 });
  assert.equal(payload.domains.length, 4);
  assert.equal(payload.domains[0].basis, "official");
  assert.equal(payload.domains[1].basis, "official");
  assert.match(payload.domains[1].detail, /列表页规模日期/);
});

test("exposes evidence levels for headline metrics", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  const metrics = Object.fromEntries(payload.metrics.map((metric) => [metric.id, metric]));
  assert.equal(metrics.etf.basis.kind, "calculated");
  assert.equal(metrics.southbound.basis.kind, "calculated");
  assert.equal(metrics.margin.basis.kind, "official");
  assert.equal(metrics.northbound.basis.kind, "official");
  assert.match(metrics.etf.note, /不是资金金额/);
});

test("exposes ETF terminal coverage without inventing unavailable periods", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/etf/flows"),
    env,
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.deepEqual(payload.terminal.availablePeriods, ["1d"]);
  assert.equal(payload.terminal.classificationVersion, "2026-07-v3");
  assert.equal(payload.terminal.groupPeriods[0].groupType, "industry");
  assert.equal(payload.terminal.instruments[0].periods["1d"], 13.98);
  assert.equal(payload.terminal.instruments[0].periods["1w"], undefined);
  assert.equal(payload.evidence.kind, "calculated");
  assert.equal(payload.evidence.isCashFlow, false);
  assert.match(payload.limitations.join(" "), /tracked-index metadata/);
});

test("restores long ETF periods from the hash-verified R2 flow state", async () => {
  const worker = await loadWorker();
  const fallbackResponse = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const snapshot = await fallbackResponse.json();
  const dates = [
    "2026-07-16",
    "2026-07-17",
    "2026-07-20",
    "2026-07-21",
    "2026-07-22",
  ];
  const state = {
    version: 1,
    classificationVersion: "2026-07-v3",
    calculationMethod: "sum_of_daily_official_share_changes",
    generatedAt: "2026-07-23T09:00:00+08:00",
    dates,
    instruments: [
      {
        code: "588170",
        name: "科创半导",
        market: "SSE",
        category: "科技",
        groupType: "industry",
        assetClass: "股票",
        classificationSource: "name_rule",
        classificationConfidence: "medium",
        classificationRule: "technology",
        includeInGroup: true,
        values: [1, 2, 3, 4, 5],
      },
    ],
    sourceSnapshots: dates.map((date) => ({ date, sha256: "a".repeat(64) })),
    quality: {
      snapshotCount: 5,
      instrumentCount: 1,
      populatedValues: 5,
      rejectedRows: 0,
      duplicateRows: 0,
      status: "pass",
    },
  };
  const body = JSON.stringify(state);
  const digest = createHash("sha256").update(body).digest("hex");
  const stateKey = `market-data/derived/etf-flow-state/2026-07-v3/2026-07-22/${digest}.json`;
  const pointer = {
    version: 1,
    classificationVersion: "2026-07-v3",
    objectKey: stateKey,
    sha256: digest,
    asOf: "2026-07-22",
    dateCount: 5,
    updatedAt: "2026-07-23T09:00:00+08:00",
  };
  snapshot.asOf = "2026-07-22";
  const db = {
    prepare() {
      return {
        async first() {
          return { payload_json: JSON.stringify(snapshot) };
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      if (key.endsWith("/latest.json")) {
        return { text: async () => JSON.stringify(pointer) };
      }
      if (key === stateKey) return { text: async () => body };
      return null;
    },
  };

  const response = await worker.fetch(
    new Request("http://localhost/api/v1/etf/flows"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.deepEqual(payload.terminal.availablePeriods, ["1d", "3d", "1w"]);
  const item = payload.terminal.instruments.find((row) => row.code === "588170");
  assert.equal(item.periods["1d"], 5);
  assert.equal(item.periods["3d"], 12);
  assert.equal(item.periods["1w"], 15);
  assert.equal(item.periodCoverage["1w"], 5);
  assert.equal(payload.terminal.historyCoverage.tradingDays, 5);
});

test("serves materialized ETF periods without re-reading the large R2 state", async () => {
  const worker = await loadWorker();
  const fallbackResponse = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const snapshot = await fallbackResponse.json();
  snapshot.etfTerminal.historyCoverage = {
    from: "2026-07-01",
    to: snapshot.asOf,
    tradingDays: 20,
    stateVersion: 1,
  };
  const db = {
    prepare() {
      return {
        async first() {
          return { payload_json: JSON.stringify(snapshot) };
        },
        async all() {
          return { results: [] };
        },
      };
    },
  };
  const bucket = {
    async get() {
      throw new Error("materialized snapshot must not hydrate R2 ETF state");
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/etf/flows"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.terminal.historyCoverage.tradingDays, 20);
});

test("labels margin fields as official balances rather than net capital flow", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/margin/flows"),
    env,
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.evidence.kind, "official");
  assert.equal(payload.coverage.points, 3);
  assert.match(payload.evidence.interpretation, /not net capital flow/);
});

test("hydrates long margin and connect series from the daily history index", async () => {
  const worker = await loadWorker();
  const fallbackResponse = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const snapshot = await fallbackResponse.json();
  snapshot.asOf = "2026-07-14";
  const rows = [
    {
      trade_date: "2024-08-01",
      margin_balance: 14000,
      margin_change: null,
      financing_buy: 700,
      lending_balance: 120,
      lending_sell_volume: 1.2,
      total_balance: 14120,
      sse_balance: 7200,
      szse_balance: 6800,
      southbound_net: null,
      southbound_turnover: null,
      southbound_sse_net: null,
      southbound_szse_net: null,
      northbound_turnover: null,
      northbound_etf_turnover: null,
      northbound_trade_count: null,
      margin_available: 1,
      connect_available: 0,
      source_snapshot_sha256: "a".repeat(64),
      observed_at: "2024-08-02T01:15:00Z",
    },
    {
      trade_date: "2026-07-14",
      margin_balance: 28692.78,
      margin_change: -109.58,
      financing_buy: 2408.55,
      lending_balance: 213.21,
      lending_sell_volume: 0.88,
      total_balance: 28905.99,
      sse_balance: 14520.14,
      szse_balance: 14172.64,
      southbound_net: 109.97,
      southbound_turnover: 1289.72,
      southbound_sse_net: 82.05,
      southbound_szse_net: 27.92,
      northbound_turnover: 4159.04,
      northbound_etf_turnover: 65.6,
      northbound_trade_count: 17561612,
      margin_available: 1,
      connect_available: 1,
      source_snapshot_sha256: "b".repeat(64),
      observed_at: "2026-07-15T01:15:00Z",
    },
  ];
  const db = {
    prepare(sql) {
      return {
        bind() { return this; },
        async first() {
          return sql.includes("FROM dashboard_snapshot")
            ? { payload_json: JSON.stringify(snapshot) }
            : null;
        },
        async all() {
          return { results: rows };
        },
      };
    },
  };
  const bucket = { async get() { return null; } };

  const marginResponse = await worker.fetch(
    new Request("http://localhost/api/v1/margin/flows"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  const marginPayload = await marginResponse.json();
  assert.equal(marginPayload.series.length, 2);
  assert.equal(marginPayload.series[0].date, "2024-08-01");
  assert.equal(marginPayload.coverage.points, 2);

  const connectResponse = await worker.fetch(
    new Request("http://localhost/api/v1/connect/flows"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  const connectPayload = await connectResponse.json();
  assert.equal(connectPayload.series.length, 1);
  assert.equal(connectPayload.series[0].date, "2026-07-14");
  assert.equal(connectPayload.coverage.points, 1);
});

test("rejects unauthenticated snapshot imports", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/admin/import", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    }),
    env,
    context,
  );
  assert.equal(response.status, 401);
});

test("keeps the official-source relay private and allowlisted", async () => {
  const worker = await loadWorker();
  const unauthenticated = await worker.fetch(
    new Request(
      "http://localhost/api/v1/admin/source-proxy?path=/commonQuery.do",
    ),
    env,
    context,
  );
  assert.equal(unauthenticated.status, 401);

  const disallowed = await worker.fetch(
    new Request(
      "http://localhost/api/v1/admin/source-proxy?path=/anything&url=https://example.com",
      { headers: { authorization: "Bearer test-key" } },
    ),
    { ...env, INGEST_API_KEY: "test-key" },
    context,
  );
  assert.equal(disallowed.status, 400);
  assert.deepEqual(await disallowed.json(), {
    error: "invalid_source_request",
    detail: "path is not allowlisted",
  });
});

test("serves an archived dashboard for an explicitly selected date", async () => {
  const worker = await loadWorker();
  const overview = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const archived = await overview.json();
  archived.asOf = "2025-01-02";
  archived.mode = "官方历史归档";
  const db = {
    prepare(sql) {
      return {
        sql,
        values: [],
        bind(...values) {
          this.values = values;
          return this;
        },
        async first() {
          if (this.sql.includes("FROM dashboard_snapshot")) return null;
          if (this.sql.includes("FROM historical_snapshot")) {
            return { as_of: "2025-01-02", r2_object_key: "market-data/history/2025-01-02/test.json" };
          }
          return null;
        },
        async all() {
          return { results: [{ as_of: "2025-01-02" }, { as_of: "2026-07-14" }] };
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      assert.equal(key, "market-data/history/2025-01-02/test.json");
      return { text: async () => JSON.stringify(archived) };
    },
  };

  const response = await worker.fetch(
    new Request("http://localhost/?date=2025-01-02", { headers: { accept: "text/html" } }),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /2025-01-02/);
  assert.match(html, /官方历史归档/);
  assert.match(html, /选择归档日期/);
});

test("archives full-source history with server-side hash verification", async () => {
  const worker = await loadWorker();
  const overview = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const snapshot = await overview.json();
  const raw = Buffer.from('{"result":[]}');
  const normalized = Buffer.from(
    "trade_date,market,financing_balance_yuan,financing_buy_yuan,securities_lending_balance_yuan,margin_total_balance_yuan,source_id,observed_at\n" +
    "2026-07-14,SSE,1452014000000,120000000000,7898999999.999999,1462014000000,sse,2026-07-22T09:15:20+08:00\n",
  );
  const connectRaw = Buffer.from("var daily = {};");
  const connectSummary = Buffer.from(
    "trade_date,market,direction,currency,total_turnover,buy_turnover,sell_turnover,net_buy,etf_turnover,source_id,observed_at\n" +
    "2026-07-14,SSE,southbound,HKD,50000000000,26000000000,24000000000,2000000000,1000000000,hkex,2026-07-22T09:15:30+08:00\n" +
    "2026-07-14,SZSE,southbound,HKD,40000000000,19000000000,21000000000,-2000000000,800000000,hkex,2026-07-22T09:15:30+08:00\n" +
    "2026-07-14,SSE,northbound,RMB,90000000000,,,,3000000000,hkex,2026-07-22T09:15:30+08:00\n" +
    "2026-07-14,SZSE,northbound,RMB,100000000000,,,,3500000000,hkex,2026-07-22T09:15:30+08:00\n",
  );
  const connectTop = Buffer.from("trade_date,market,direction,rank,instrument_code,instrument_name,total_turnover,source_id,observed_at\n");
  const sha256 = createHash("sha256").update(raw).digest("hex");
  const normalizedSha256 = createHash("sha256").update(normalized).digest("hex");
  const connectSha256 = createHash("sha256").update(connectRaw).digest("hex");
  const connectSummarySha256 = createHash("sha256").update(connectSummary).digest("hex");
  const connectTopSha256 = createHash("sha256").update(connectTop).digest("hex");
  const statements = [];
  const objects = new Map();
  const db = {
    prepare(sql) {
      return {
        sql,
        values: [],
        bind(...values) {
          this.values = values;
          return this;
        },
        async run() {
          statements.push({ sql: this.sql, values: this.values });
          return { success: true };
        },
        async all() {
          statements.push({ sql: this.sql, values: this.values });
          return { results: [] };
        },
      };
    },
  };
  const bucket = {
    async put(key, value) {
      objects.set(key, value);
      return {};
    },
    async head(key) {
      return objects.has(key) ? { customMetadata: {} } : null;
    },
  };
  const bundle = {
    version: 1,
    history: {
      runId: "history-20260722T091500+0800",
      startedAt: "2026-07-22T09:15:00+08:00",
      finishedAt: "2026-07-22T09:16:00+08:00",
      retentionDays: 0,
      asOf: snapshot.asOf,
      snapshot,
      artifacts: [
        {
          releaseId: `margin_market_daily:sse:2026-07-14:${sha256.slice(0, 16)}`,
          sourceId: "sse",
          dataset: "margin_market_daily",
          requestedDate: "2026-07-14",
          tradeDates: ["2026-07-14"],
          observedAt: "2026-07-22T09:15:20+08:00",
          sourceUrl: "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do",
          dateBasis: "source_published",
          sha256,
          contentType: "application/json",
          extension: "json",
          rawContentBase64: raw.toString("base64"),
          metadata: { sha256 },
          normalized: [
            {
              name: "data",
              format: "csv",
              sha256: normalizedSha256,
              rowCount: 1,
              tradeDates: ["2026-07-14"],
              contentBase64: normalized.toString("base64"),
            },
          ],
        },
        {
          releaseId: `stock_connect_daily:hkex:2026-07-14:${connectSha256.slice(0, 16)}`,
          sourceId: "hkex",
          dataset: "stock_connect_daily",
          requestedDate: "2026-07-14",
          tradeDates: ["2026-07-14"],
          observedAt: "2026-07-22T09:15:30+08:00",
          sourceUrl: "https://www.hkex.com.hk/chi/csm/DailyStat/data_tab_daily_20260714c.js",
          dateBasis: "source_published",
          sha256: connectSha256,
          contentType: "application/javascript",
          extension: "js",
          rawContentBase64: connectRaw.toString("base64"),
          metadata: { sha256: connectSha256 },
          normalized: [
            {
              name: "summary",
              format: "csv",
              sha256: connectSummarySha256,
              rowCount: 4,
              tradeDates: ["2026-07-14"],
              contentBase64: connectSummary.toString("base64"),
            },
            {
              name: "top",
              format: "csv",
              sha256: connectTopSha256,
              rowCount: 0,
              tradeDates: [],
              contentBase64: connectTop.toString("base64"),
            },
          ],
        },
      ],
    },
  };
  bundle.history.expectedArtifacts = bundle.history.artifacts.map(
    (artifact) => artifact.releaseId,
  );
  bundle.history.finalize = true;
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/admin/history-import", {
      method: "POST",
      headers: { authorization: "Bearer test-key", "content-type": "application/json" },
      body: JSON.stringify(bundle),
    }),
    { ...env, DB: db, RAW_BUCKET: bucket, INGEST_API_KEY: "test-key" },
    context,
  );
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "archived");
  assert.equal(result.standardizedRows, 5);
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/raw/sse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/normalized/sse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/history/")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO historical_snapshot")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO market_history_daily")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO margin_daily")));
  assert.ok(statements.some(
    ({ sql, values }) =>
      sql.includes("INSERT INTO margin_daily") &&
      values.includes(7_899_000_000),
  ));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO connect_daily")));
  assert.ok(statements.some(({ sql }) => sql.includes("WITH current_rows AS")));
});

test("archives verified ETF evidence before publishing a snapshot", async () => {
  const worker = await loadWorker();
  const overview = await worker.fetch(
    new Request("http://localhost/api/v1/overview"),
    env,
    context,
  );
  const snapshot = await overview.json();
  const statements = [];
  const objects = new Map();
  const db = {
    prepare(sql) {
      const statement = {
        sql,
        values: [],
        bind(...values) {
          this.values = values;
          return this;
        },
        async first() {
          statements.push({ sql: this.sql, values: this.values, method: "first" });
          return null;
        },
        async run() {
          statements.push({ sql: this.sql, values: this.values, method: "run" });
          return { success: true };
        },
      };
      return statement;
    },
    async batch(batchStatements) {
      for (const statement of batchStatements) await statement.run();
      return batchStatements.map(() => ({ success: true }));
    },
  };
  const bucket = {
    async put(key, value) {
      objects.set(key, value);
      return {};
    },
  };
  const raw = Buffer.from('{"result":[]}');
  const sha256 = createHash("sha256").update(raw).digest("hex");
  const bundle = {
    version: 1,
    snapshot,
    archive: {
      runId: "20260722T091500+0800",
      startedAt: "2026-07-22T09:15:00+08:00",
      finishedAt: "2026-07-22T09:16:00+08:00",
      retentionDays: 365,
      artifacts: [
        {
          releaseId: `etf:sse:2026-07-21:${sha256.slice(0, 16)}`,
          sourceId: "sse",
          market: "SSE",
          tradeDate: "2026-07-21",
          observedAt: "2026-07-22T09:15:20+08:00",
          sourceUrl: "https://query.sse.com.cn/commonQuery.do",
          dateBasis: "source_published",
          sha256,
          contentType: "application/json",
          extension: "json",
          rawContentBase64: raw.toString("base64"),
          metadata: { sha256 },
          rows: [
            {
              code: "510300",
              name: "300ETF",
              category: "股票ETF",
              shares: 9876543200,
              navMicros: null,
              dateBasis: "source_published",
            },
          ],
        },
      ],
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/admin/import", {
      method: "POST",
      headers: {
        authorization: "Bearer test-key",
        "content-type": "application/json",
      },
      body: JSON.stringify(bundle),
    }),
    { ...env, DB: db, RAW_BUCKET: bucket, INGEST_API_KEY: "test-key" },
    context,
  );

  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.archivedReleases, 1);
  assert.equal(result.archivedRows, 1);
  assert.ok([...objects.keys()].some((key) => key.startsWith("market-data/raw/sse/")));
  assert.ok([...objects.keys()].some((key) => key.startsWith("market-data/manifests/")));
  assert.ok([...objects.keys()].some((key) => key.startsWith("dashboard/")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO etf_daily")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO dashboard_snapshot")));
});

test("blocks failed snapshots from replacing the active dashboard", () => {
  assert.equal(
    isPublishableSnapshot({ gate: "conditional_pass", quality: { fail: 0 } }),
    true,
  );
  assert.equal(
    isPublishableSnapshot({ gate: "blocked", quality: { fail: 0 } }),
    false,
  );
  assert.equal(
    isPublishableSnapshot({ gate: "conditional_pass", quality: { fail: 1 } }),
    false,
  );
});
