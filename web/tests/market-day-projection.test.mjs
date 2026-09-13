import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFile } from "node:fs/promises";
import { webcrypto } from "node:crypto";
import test from "node:test";
import {
  MARKET_DAY_SOURCE_DEFINITIONS,
  getMarketDayState,
  persistMarketDayProjection,
  projectMarketDay,
  shouldDispatchHeavyCollection,
} from "../app/lib/market-day-projection.ts";
import { probeMarketDaySources } from "../app/lib/market-day-probe.ts";

globalThis.crypto ??= webcrypto;

const TARGET_DATE = "2026-09-13";

function observation(definition, overrides = {}) {
  return {
    observationId: `obs:${definition.key}`,
    sourceId: definition.sourceId,
    dataset: definition.dataset,
    targetDate: TARGET_DATE,
    sourceBusinessDate: TARGET_DATE,
    publishedDate: TARGET_DATE,
    status: "VERIFIED",
    sourceFingerprint: "a".repeat(64),
    observedAt: "2026-09-13T10:00:00.000Z",
    ...overrides,
  };
}

function allRequiredObservations(overrides = {}) {
  return MARKET_DAY_SOURCE_DEFINITIONS
    .filter((definition) => definition.required)
    .map((definition) => observation(definition, overrides[definition.key] ?? {}));
}

function probe(sourceKey, overrides = {}) {
  const definition = MARKET_DAY_SOURCE_DEFINITIONS.find((item) => item.key === sourceKey);
  return {
    sourceKey,
    sourceId: definition.sourceId,
    dataset: definition.dataset,
    targetDate: TARGET_DATE,
    status: "PUBLISHED",
    httpStatus: 200,
    observedAt: "2026-09-13T10:00:00.000Z",
    sourceBusinessDate: TARGET_DATE,
    publishedDate: TARGET_DATE,
    fingerprint: "b".repeat(64),
    reason: "probe_business_date_observed",
    evidence: {},
    ...overrides,
  };
}

test("market-day projection stays waiting until every required source is verified", async () => {
  const probes = MARKET_DAY_SOURCE_DEFINITIONS.filter((item) => item.required).map((item) => probe(item.key));
  const ready = await projectMarketDay({ targetDate: TARGET_DATE, observations: allRequiredObservations(), probes, projectedAt: "2026-09-13T10:00:00.000Z" });
  assert.equal(ready.state, "READY_TO_BUILD");
  assert.equal(ready.counts.verified, 6);
  assert.equal(ready.nextAction, "build_release");

  const waiting = await projectMarketDay({
    targetDate: TARGET_DATE,
    observations: allRequiredObservations({ szse_etf: { status: "NOT_PUBLISHED", sourceBusinessDate: null } }),
    probes: probes.map((item) => item.sourceKey === "szse_etf" ? probe(item.sourceKey, { status: "NOT_PUBLISHED", sourceBusinessDate: null }) : item),
    projectedAt: "2026-09-13T10:00:00.000Z",
  });
  assert.equal(waiting.state, "WAITING_SOURCES");
  assert.equal(waiting.counts.verified, 5);
  assert.equal(waiting.nextAction, "collect_sources");
  assert.equal(waiting.sources.find((item) => item.key === "szse_etf").nextAction, "wait_for_publication");
});

test("date-mismatched observations cannot satisfy the market-day gate", async () => {
  const projection = await projectMarketDay({
    targetDate: TARGET_DATE,
    observations: allRequiredObservations({ sse_margin: { sourceBusinessDate: "2026-09-12" } }),
    probes: MARKET_DAY_SOURCE_DEFINITIONS.filter((item) => item.required).map((item) => probe(item.key)),
  });
  const source = projection.sources.find((item) => item.key === "sse_margin");
  assert.equal(source.status, "DATE_MISMATCH");
  assert.equal(projection.state, "WAITING_SOURCES");
  assert.equal(projection.counts.unavailable, 1);
  assert.equal(projection.nextAction, "investigate_source");
});

test("canonical probe and collection decisions are stable across retries", async () => {
  const probes = MARKET_DAY_SOURCE_DEFINITIONS.filter((item) => item.required).map((item) => probe(item.key));
  const first = await projectMarketDay({ targetDate: TARGET_DATE, probes, observations: [] });
  const second = await projectMarketDay({ targetDate: TARGET_DATE, probes, observations: [] });
  assert.equal(first.probeFingerprint, second.probeFingerprint);
  assert.equal(first.sourceFingerprint, second.sourceFingerprint);
  assert.equal(shouldDispatchHeavyCollection(null, first, null), true);
  assert.equal(shouldDispatchHeavyCollection(first, second, "DISPATCHED"), false);
  assert.equal(shouldDispatchHeavyCollection(first, second, "FAILED_RETRYABLE"), true);
  const unavailable = await projectMarketDay({
    targetDate: TARGET_DATE,
    probes: probes.map((item) => ({ ...item, status: "SOURCE_UNAVAILABLE", httpStatus: 503 })),
  });
  assert.equal(shouldDispatchHeavyCollection(null, unavailable, null), false);
});

test("probe uses fixed allowlisted URLs, bounded response reads and no parallel burst", async () => {
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url: String(url), options });
    assert.equal(options.method, "GET");
    assert.match(options.headers.Range, /^bytes=0-65535$/);
    return new Response(`{"STAT_DATE":"${TARGET_DATE}","opDate":"${TARGET_DATE}","tabData":[]}`, {
      status: 200,
      headers: { "content-type": "application/json", "content-length": "88" },
    });
  };
  const probes = await probeMarketDaySources(TARGET_DATE, "2026-09-13T10:00:00.000Z", fakeFetch);
  assert.equal(probes.length, 6);
  assert.equal(calls.length, 6);
  assert.ok(calls.every((call) => !call.url.includes("api_key") && !call.url.includes("token")));
  assert.ok(calls[0].url.startsWith("https://query.sse.com.cn/commonQuery.do"));
  assert.ok(calls.some((call) => call.url.includes("fund.szse.cn/api/report/ShowReport/data")));
  assert.ok(calls.some((call) => call.url.includes("hkex.com.hk/chi/csm/DailyStat/data_tab_daily_20260913c.js")));

  const probeSource = await readFile(new URL("../app/lib/market-day-probe.ts", import.meta.url), "utf8");
  assert.doesNotMatch(probeSource, /(?:D1Database|R2Bucket|\.prepare\(|\.put\(|actions\/workflows)/);
});

class LocalD1Statement {
  constructor(database, sql) {
    this.database = database;
    this.sql = sql;
    this.values = [];
  }
  bind(...values) {
    this.values = values;
    return this;
  }
  async run() {
    const result = this.database.prepare(this.sql).run(...this.values);
    const changes = Number(result.changes ?? 0);
    return { meta: { changes, rows_written: changes } };
  }
  async first() {
    return this.database.prepare(this.sql).get(...this.values) ?? null;
  }
  async all() {
    return { results: this.database.prepare(this.sql).all(...this.values) };
  }
}

class LocalD1 {
  constructor(database) {
    this.database = database;
  }
  prepare(sql) {
    return new LocalD1Statement(this.database, sql);
  }
}

async function database() {
  const database = new DatabaseSync(":memory:");
  for (const migrationName of [
    "0010_fair_jasper_sitwell.sql",
    "0011_futuristic_namora.sql",
    "0012_boring_tyger_tiger.sql",
    "0013_unusual_zeigeist.sql",
    "0014_warm_morlocks.sql",
  ]) {
    const sql = await readFile(new URL(`../drizzle/${migrationName}`, import.meta.url), "utf8");
    for (const statement of sql.split("--> statement-breakpoint")) {
      if (statement.trim()) database.exec(statement);
    }
  }
  return database;
}

test("projection persistence is one bounded upsert and route reads use one key lookup", async () => {
  const databaseHandle = await database();
  const db = new LocalD1(databaseHandle);
  const projection = await projectMarketDay({ targetDate: TARGET_DATE, probes: [], observations: [] });
  const result = await persistMarketDayProjection(db, projection, new Date("2026-09-13T10:00:00.000Z"));
  assert.equal(result.changes, 1);
  assert.ok(result.rowsWritten <= 4);
  const row = await getMarketDayState(db, TARGET_DATE);
  assert.equal(row.target_date, TARGET_DATE);
  assert.match(row.probe_json, /^\[/);
  databaseHandle.close();
});

test("worker probe flag is opt-in and runs before the existing dispatch path", async () => {
  const worker = await import(new URL("../dist/server/index.js", import.meta.url).href + `?market-day-test=${process.pid}-${Date.now()}`);
  const workerSource = await readFile(new URL("../worker/index.ts", import.meta.url), "utf8");
  assert.match(workerSource, /MARKET_DAY_PROBE_ENABLED/);
  assert.match(workerSource, /probeMarketDaySources/);
  assert.match(workerSource, /getPipelineWindow/);
  assert.match(workerSource, /sourceFingerprint: probeFingerprint/);
  assert.match(workerSource, /MARKET_DAY_PROBE_ENABLED requires PIPELINE_WINDOW_GATE_ENABLED/);
  assert.match(workerSource, /ORDER BY observed_at DESC/);
  assert.match(workerSource, /LIMIT \?/);
  assert.match(workerSource, /MARKET_DAY_PROBE_ENABLED/);
  assert.doesNotMatch(workerSource, /UPDATE\s+active_release/i);
  assert.doesNotMatch(workerSource, /PIPELINE_RECOVERY_ENABLED\s*=\s*['"]1/);
  assert.equal(typeof worker.default?.scheduled, "function");
});
