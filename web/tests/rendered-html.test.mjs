import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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
  assert.match(html, /个人市场数据交易台/);
  assert.match(html, /板块份额变化/);
  assert.match(html, /ETF 雷达/);
  assert.match(html, /南向净买入/);
  assert.match(html, /ETF 份额变化/);
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
  assert.equal(payload.terminal.instruments[0].periods["1d"], 13.98);
  assert.equal(payload.terminal.instruments[0].periods["1w"], undefined);
  assert.equal(payload.evidence.kind, "calculated");
  assert.equal(payload.evidence.isCashFlow, false);
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
  assert.match(payload.evidence.interpretation, /not net capital flow/);
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
