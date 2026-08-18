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

test("serves a static dashboard shell", async () => {
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
  assert.match(html, /bootstrap-card/);
  assert.match(html, /只读取已经通过 QA 的完整数据/);
  assert.match(html, /dashboard-bootstrap/);
  assert.doesNotMatch(html, /啸傲的市场资金流向交易台/);
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
  const [source, bootstrapSource, configText] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/dashboard-bootstrap.tsx", import.meta.url), "utf8"),
    readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"),
  ]);
  const config = JSON.parse(configText);
  assert.match(source, /AUTO_REFRESH_MS = 30 \* 60 \* 1000/);
  assert.match(source, /window\.setInterval\(refresh, AUTO_REFRESH_MS\)/);
  assert.match(source, /if \(!autoRefresh \|\| requestedDate\) return/);
  assert.match(source, /onAutoRefresh/);
  assert.doesNotMatch(source, /if \(document\.visibilityState === "visible"\) window\.location\.reload\(\);/);
  assert.match(bootstrapSource, /\/api\/v2\/releases\/latest/);
  assert.match(bootstrapSource, /release_pending/);
  assert.match(bootstrapSource, /REFRESH_ERROR_THRESHOLD = 3/);
  assert.match(bootstrapSource, /REQUEST_TIMEOUT_MS = 15_000/);
  assert.match(bootstrapSource, /浏览器读取发布指针连续失败/);
  assert.match(source, /发布检查暂不可用/);
  assert.match(source, /当前仍展示.*release/);
  assert.match(source, /lastCheck = Date\.now\(\);[\s\S]*onAutoRefresh/);
  assert.match(bootstrapSource, /\/api\/v2\/views\/\$\{view\}/);
  assert.match(bootstrapSource, /viewReleaseId/);
  assert.match(source, /onViewChange/);
  assert.doesNotMatch(source, /window\.location\.reload\(\)/);
  assert.match(bootstrapSource, /cache: "no-store"/);
  assert.deepEqual(config.triggers.crons, [
    "30 10 * * mon-fri",
    "45 23 * * sun-thu",
    "45 0 * * mon-fri",
  ]);
});

test("does not let the outer Worker overwrite an injected build identity", async () => {
  const worker = await readFile(new URL("../worker/index.ts", import.meta.url), "utf8");
  assert.match(worker, /const existing = headers\.get\(name\)/);
  assert.match(worker, /if \(!existing && value !== "local"\) headers\.set\(name, value\)/);
});

test("keeps release history and rollback reads exact and migration-compatible", async () => {
  const [contract, capabilities, candidates, artifact, dates, importer] = await Promise.all([
    readFile(new URL("../app/lib/read-model-contract.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v1/admin/capabilities/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v1/admin/releases/previous-candidates/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v1/admin/releases/artifact/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/dashboard-store.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v1/admin/import/route.ts", import.meta.url), "utf8"),
  ]);
  assert.match(contract, /schemaVersion !== 2 && value\.schemaVersion !== 3/);
  assert.match(contract, /requiredArtifacts/);
  assert.match(capabilities, /readModelManifestSchemas/);
  assert.match(capabilities, /sameDateObservedAtOrder/);
  assert.match(candidates, /MAX_CANDIDATES = 30/);
  assert.match(candidates, /status: candidates\.length \? "found" : "not_found"/);
  assert.match(artifact, /releaseId = url\.searchParams\.get\("releaseId"\)/);
  assert.match(artifact, /WHERE release_id = \?/);
  assert.match(dates, /FROM release_manifest/);
  assert.match(importer, /observed_at/);
  assert.match(importer, /staged_stale_revision/);
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

test("exposes a versioned ETF-to-stock search and detail contract", async () => {
  const [dashboard, bootstrap, detail, releaseStore, searchRoute, shardRoute, readModels, searchUtils] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/dashboard-bootstrap.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/stock-detail-view.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/release-store.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v2/stock-search/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v2/stocks/[shard]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../../src/trading_desk/read_models.py", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/search-utils.ts", import.meta.url), "utf8"),
  ]);
  assert.match(dashboard, /搜索 ETF \/ 个股 \/ 代码/);
  assert.match(dashboard, /openStockDetail/);
  assert.match(bootstrap, /api\/v2\/views\/\$\{view\}/);
  assert.match(dashboard, /onLoadRadarFlow/);
  assert.match(dashboard, /点击个股查看完整贡献拆解/);
  assert.match(dashboard, /x-flow-release-id/);
  assert.match(dashboard, /url\.searchParams\.set\("date", snapshot\.asOf\)/);
  assert.match(dashboard, /disabled=\{!activeAvailablePeriods\.has\(item\.id\)\}/);
  assert.match(bootstrap, /params\.get\("view"\) === "stock"/);
  assert.match(bootstrap, /viewAsOf/);
  assert.match(detail, /只读取已通过 QA 的同一发布版本/);
  assert.match(detail, /legacy_radar_embedded_stock_flow/);
  assert.match(detail, /embeddedStockDetail/);
  assert.match(detail, /ETF 份额变化 ÷ 最小申赎单位 × PCF 成分数量/);
  assert.match(detail, /历史节点积累中/);
  assert.match(releaseStore, /getReleaseManifestForDate/);
  assert.match(releaseStore, /readPublishedArtifact/);
  assert.match(searchRoute, /readPublishedArtifact\("stock_search"/);
  assert.match(shardRoute, /stock_\[0-7\]/);
  assert.match(readModels, /stock_catalog_required_for_available_flow/);
  assert.match(searchUtils, /replace\(\/\\s\+\/g, ""\)/);
});

test("exposes the ETF pressure workbench without fabricating stock facts", async () => {
  const [source, bootstrap, route, pressure, types] = await Promise.all([
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/dashboard-bootstrap.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v2/views/[view]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/etf-pressure-view.tsx", import.meta.url), "utf8"),
    readFile(new URL("../../src/trading_desk/stock_pressure.py", import.meta.url), "utf8"),
  ]);
  assert.match(source, /id: "pressure", label: "ETF β压强"/);
  assert.match(source, /<EtfPressureView data=\{snapshot\.pressure\}/);
  assert.match(bootstrap, /requestedView\(params\.get\("view"\)\)/);
  assert.match(bootstrap, /\/api\/v2\/views\/\$\{view\}/);
  assert.match(route, /"pressure"/);
  assert.match(route, /getReleaseManifestForDate/);
  assert.match(pressure, /ETF → 个股 · 申赎篮子等价/);
  assert.match(pressure, /市场融资余额/);
  assert.match(pressure, /历史节点不足/);
  assert.match(pressure, /window\.history\.replaceState/);
  assert.match(pressure, /ETF 申赎篮子穿透是估算等价/);
  assert.match(types, /marketFinancingBalanceYuan/);
  assert.match(types, /pressure_flow_identity_mismatch/);
});

test("requires explicit confirmation before opening a historical date", async () => {
  const source = await readFile(
    new URL("../app/dashboard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /setDraftDate\(event\.target\.value\)/);
  assert.match(source, />\s*查看\s*<\/button>/);
  assert.match(source, /if \(!availableDates\.includes\(date\)\)/);
  assert.match(source, /url\.searchParams\.set\("date", date\)/);
  assert.match(source, /const latest = availableDates\.at\(-1\)/);
  assert.match(source, /if \(date === latest\) url\.searchParams\.delete\("date"\)/);
  assert.doesNotMatch(source, /onChange=\{\(event\) => event\.target\.value && openDate/);
  assert.match(source, /data-status-strip/);
  assert.match(source, /等待完整发布/);
  assert.match(source, /完整发布已确认/);

  const importSource = await readFile(
    new URL("../app/api/v1/admin/import/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(importSource, /staged_older_than_active/);
  assert.match(importSource, /WHERE excluded\.as_of >= active_release\.as_of/);
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

test("does not hydrate the large R2 ETF state in the read path", async () => {
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
  snapshot.asOf = dates.at(-1);
  let bucketReads = 0;
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
      bucketReads += 1;
      throw new Error("read path must not hydrate ETF state");
    },
  };

  const response = await worker.fetch(
    new Request("http://localhost/api/v1/etf/flows"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.deepEqual(payload.terminal.availablePeriods, ["1d"]);
  assert.equal(bucketReads, 0);
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

  const invalidBse = await worker.fetch(
    new Request(
      "http://localhost/api/v1/admin/source-proxy?path=/rzrqjyyexxController/summaryInfoResult.do&callback=cb&transDate=2026-08-17&page=1",
      { headers: { authorization: "Bearer test-key" } },
    ),
    { ...env, INGEST_API_KEY: "test-key" },
    context,
  );
  assert.equal(invalidBse.status, 400);
  assert.deepEqual(await invalidBse.json(), {
    error: "invalid_source_request",
    detail: "page must be 0",
  });
});

test("streams an archived dashboard for an explicitly selected date", async () => {
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
      return {
        body: new Response(JSON.stringify(archived)).body,
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        httpEtag: '"archived-test"',
        customMetadata: { asOf: "2025-01-02" },
      };
    },
  };

  const response = await worker.fetch(
    new Request("http://localhost/api/v2/dashboard?date=2025-01-02"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-flow-as-of"), "2025-01-02");
  assert.equal(response.headers.get("x-flow-historical"), "true");
  const streamed = await response.json();
  assert.equal(streamed.asOf, "2025-01-02");
});

test("streams the active release through the D1 pointer without parsing it", async () => {
  const worker = await loadWorker();
  const snapshot = {
    asOf: "2026-08-06",
    mode: "官方数据快照",
    quality: { fail: 0 },
  };
  const manifest = {
    schemaVersion: 2,
    releaseId: "release:2026-08-06:0123456789abcdef",
    asOf: "2026-08-06",
    artifacts: {
      bootstrap: {
        key: "market-data/releases/2026-08-06/release:2026-08-06:0123456789abcdef/bootstrap.json",
      },
    },
  };
  const db = {
    prepare(sql) {
      return {
        bind() {
          return this;
        },
        async first() {
          if (sql.includes("active_release")) {
            return {
              release_id: manifest.releaseId,
              as_of: manifest.asOf,
              manifest_sha256: "a".repeat(64),
              activated_at: "2026-08-06T09:00:00+08:00",
              manifest_json: JSON.stringify(manifest),
            };
          }
          return null;
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      assert.equal(key, manifest.artifacts.bootstrap.key);
      return {
        body: new Response(JSON.stringify(snapshot)).body,
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        httpEtag: '"active-release"',
        customMetadata: { asOf: manifest.asOf, releaseId: manifest.releaseId },
      };
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v2/dashboard"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-flow-release-id"), manifest.releaseId);
  assert.match(response.headers.get("x-flow-build-sha") ?? "", /^(local|[0-9a-f]{40})$/);
  assert.match(response.headers.get("x-flow-deployed-at") ?? "", /^(local|\d{4}-\d{2}-\d{2}T)/);
  assert.deepEqual(await response.json(), snapshot);
});

test("routes an explicit active date to the complete active release", async () => {
  const worker = await loadWorker();
  const snapshot = {
    asOf: "2026-08-06",
    mode: "官方数据快照",
    quality: { fail: 0 },
    etfTerminal: { availablePeriods: ["1d", "3d", "1w"] },
  };
  const manifest = {
    schemaVersion: 2,
    releaseId: "release:2026-08-06:fedcba9876543210",
    asOf: "2026-08-06",
    artifacts: {
      bootstrap: {
        key: "market-data/releases/2026-08-06/release:2026-08-06:fedcba9876543210/bootstrap.json",
      },
    },
  };
  const db = {
    prepare(sql) {
      return {
        bind() { return this; },
        async first() {
          if (sql.includes("active_release")) {
            return {
              release_id: manifest.releaseId,
              as_of: manifest.asOf,
              manifest_sha256: "b".repeat(64),
              activated_at: "2026-08-06T09:00:00+08:00",
              manifest_json: JSON.stringify(manifest),
            };
          }
          throw new Error("historical archive must not be queried for active date");
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      assert.equal(key, manifest.artifacts.bootstrap.key);
      return {
        body: new Response(JSON.stringify(snapshot)).body,
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        httpEtag: '"active-date"',
        customMetadata: { asOf: manifest.asOf, releaseId: manifest.releaseId },
      };
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v2/dashboard?date=2026-08-06"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-flow-historical"), "false");
  assert.deepEqual((await response.json()).etfTerminal.availablePeriods, ["1d", "3d", "1w"]);
});

test("streams the stock search artifact from the requested immutable release", async () => {
  const worker = await loadWorker();
  const activeReleaseId = "release:2026-08-07:0123456789abcdef";
  const datedReleaseId = "release:2026-08-06:fedcba9876543210";
  const stockKey = `market-data/releases/2026-08-06/${datedReleaseId}/stock_search.json`;
  const manifest = {
    schemaVersion: 2,
    releaseId: datedReleaseId,
    asOf: "2026-08-06",
    artifacts: { stock_search: { key: stockKey } },
  };
  const payload = { schemaVersion: 1, asOf: "2026-08-06", items: [] };
  const db = {
    prepare(sql) {
      return {
        bind() { return this; },
        async first() {
          if (sql.includes("active_release")) {
            return {
              release_id: activeReleaseId,
              as_of: "2026-08-07",
              manifest_sha256: "a".repeat(64),
              activated_at: "2026-08-07T09:00:00+08:00",
              manifest_json: JSON.stringify({
                schemaVersion: 2,
                releaseId: activeReleaseId,
                asOf: "2026-08-07",
                artifacts: {},
              }),
            };
          }
          if (sql.includes("FROM release_manifest")) {
            return {
              release_id: datedReleaseId,
              as_of: "2026-08-06",
              manifest_sha256: "b".repeat(64),
              created_at: "2026-08-06T09:00:00+08:00",
              manifest_json: JSON.stringify(manifest),
            };
          }
          return null;
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      assert.equal(key, stockKey);
      return {
        body: new Response(JSON.stringify(payload)).body,
        httpMetadata: { contentType: "application/json; charset=utf-8" },
        httpEtag: '"stock-search"',
        customMetadata: { asOf: "2026-08-06", releaseId: datedReleaseId },
      };
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v2/stock-search?date=2026-08-06"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-flow-release-id"), datedReleaseId);
  assert.equal(response.headers.get("x-flow-artifact"), "stock_search");
  assert.deepEqual(await response.json(), payload);
});

test("fails closed when the active release object is missing", async () => {
  const worker = await loadWorker();
  const db = {
    prepare(sql) {
      return {
        bind() {
          return this;
        },
        async first() {
          if (sql.includes("active_release")) {
            return {
              release_id: "release:2026-08-07:0123456789abcdef",
              as_of: "2026-08-07",
              manifest_sha256: "a".repeat(64),
              activated_at: "2026-08-07T09:00:00+08:00",
              manifest_json: JSON.stringify({
                releaseId: "release:2026-08-07:0123456789abcdef",
                asOf: "2026-08-07",
                artifacts: {
                  bootstrap: {
                    key: "market-data/releases/2026-08-07/release:2026-08-07:0123456789abcdef/bootstrap.json",
                  },
                },
              }),
            };
          }
          if (sql.includes("dashboard_snapshot")) {
            return {
              id: "old-dashboard",
              as_of: "2026-08-06",
              r2_object_key: "dashboard/2026-08-06/old.json",
            };
          }
          return null;
        },
      };
    },
  };
  const bucket = {
    async get(key) {
      assert.match(key, /2026-08-07/);
      return null;
    },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v2/dashboard"),
    { ...env, DB: db, RAW_BUCKET: bucket },
    context,
  );
  assert.equal(response.status, 503);
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
  const bseRaw = Buffer.from("null([], '2026-07-14')");
  const bseNormalized = Buffer.from(
    "trade_date,market,financing_balance_yuan,financing_buy_yuan,securities_lending_balance_yuan,margin_total_balance_yuan,source_id,observed_at\n" +
    "2026-07-14,BSE,8210630112,433272098,454583,8211084695,bse,2026-07-22T09:15:20+08:00\n",
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
  const bseSha256 = createHash("sha256").update(bseRaw).digest("hex");
  const bseNormalizedSha256 = createHash("sha256").update(bseNormalized).digest("hex");
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
          releaseId: `margin_market_daily:bse:2026-07-14:${bseSha256.slice(0, 16)}`,
          sourceId: "bse",
          dataset: "margin_market_daily",
          requestedDate: "2026-07-14",
          tradeDates: ["2026-07-14"],
          observedAt: "2026-07-22T09:15:20+08:00",
          sourceUrl: "https://www.bse.cn/rzrqjyyexxController/summaryInfoResult.do",
          dateBasis: "bse_official_summary_trade_date",
          sha256: bseSha256,
          contentType: "application/json",
          extension: "json",
          rawContentBase64: bseRaw.toString("base64"),
          metadata: { sha256: bseSha256 },
          normalized: [
            {
              name: "data",
              format: "csv",
              sha256: bseNormalizedSha256,
              rowCount: 1,
              tradeDates: ["2026-07-14"],
              contentBase64: bseNormalized.toString("base64"),
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
  assert.equal(result.standardizedRows, 6);
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/raw/sse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/normalized/sse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/raw/bse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/normalized/bse/margin_market_daily")));
  assert.ok([...objects.keys()].some((key) => key.includes("market-data/history/")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO historical_snapshot")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO market_history_daily")));
  assert.ok(statements.some(({ sql }) => sql.includes("INSERT INTO margin_daily")));
  assert.ok(statements.some(
    ({ sql, values }) =>
      sql.includes("INSERT INTO margin_daily") &&
      values.includes(7_899_000_000),
  ));
  assert.ok(statements.some(
    ({ sql, values }) =>
      sql.includes("INSERT INTO margin_daily") &&
      values.includes(8_210_630_112),
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
