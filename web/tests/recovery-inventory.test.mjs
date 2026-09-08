import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  collectRecoveryInventory,
  decodeInventoryCursor,
  decodeR2InventoryCursor,
  encodeInventoryCursor,
  encodeR2InventoryCursor,
  INVENTORY_STAGES,
  isRecoveryInventoryAuthorized,
  MAX_DATE_SPAN_DAYS,
  MAX_D1_BOUND_PARAMETERS,
  MAX_D1_ROWS_READ,
  MAX_INVENTORY_LIMIT,
  MAX_R2_HEADS,
  MAX_R2_CURSOR_LENGTH,
  parseInventoryQuery,
} from "../app/lib/recovery-inventory.ts";
import {
  MARKET_READ_MODEL_NAMES,
  PENETRATION_READ_MODEL_NAMES,
} from "../app/lib/read-model-contract.ts";

const SHA = (letter) => letter.repeat(64);

class FakeD1 {
  constructor({ dates = [], releases = [], readiness = [], pointers = [], models = [], dashboards = [], historical = [], pipelines = [], recoveries = [], activeReleases = [], rowsWritten = 0, rowsRead = 1, missingMeta = false, failOn = null } = {}) {
    this.fixture = { dates, releases, readiness, pointers, models, dashboards, historical, pipelines, recoveries, activeReleases };
    this.rowsWritten = rowsWritten;
    this.rowsRead = rowsRead;
    this.missingMeta = missingMeta;
    this.failOn = failOn;
    this.calls = [];
  }

  prepare = (sql) => ({
      bind: (...values) => ({
        all: async () => {
            this.calls.push({ sql, values });
            if (this.failOn && sql.includes(this.failOn)) throw new Error("injected_d1_failure");
            let results = [];
            if (sql.includes("WITH dates")) {
              const cursor = String(values.at(-2) ?? "");
              const limit = Number(values.at(-1));
              results = this.fixture.dates.filter((date) => date > cursor).slice(0, limit).map((data_date) => ({ data_date }));
            } else if (sql.includes("FROM release_manifest")) results = this.fixture.releases;
            else if (sql.includes("FROM source_readiness")) results = this.fixture.readiness;
            else if (sql.includes("FROM history_artifact_pointer")) results = this.fixture.pointers;
            else if (sql.includes("FROM etf_daily") || sql.includes("FROM margin_daily") || sql.includes("FROM connect_daily")) results = this.fixture.models;
            else if (sql.includes("FROM pipeline_window_recovery")) results = this.fixture.recoveries;
            else if (sql.includes("FROM pipeline_window")) results = this.fixture.pipelines;
            else if (sql.includes("FROM active_release")) results = this.fixture.activeReleases;
            else if (sql.includes("FROM dashboard_snapshot")) results = this.fixture.dashboards;
            else if (sql.includes("FROM historical_snapshot")) results = this.fixture.historical;
            return this.missingMeta ? { results } : { results, meta: { rows_read: this.rowsRead, rows_written: this.rowsWritten } };
          },
      }),
  });
}

class FakeR2 {
  constructor(objects = {}, { failure = null } = {}) {
    this.objects = objects;
    this.failure = failure;
    this.heads = [];
    this.list = () => { throw new Error("list_must_not_be_called"); };
    this.get = () => { throw new Error("get_must_not_be_called"); };
  }

  async head(key) {
    this.heads.push(key);
    if (this.failure) throw new Error(this.failure);
    return this.objects[key] ?? null;
  }
}

function descriptor(name, asOf, releaseId, sha = SHA("a")) {
  return {
    key: `market-data/releases/${asOf}/${releaseId}/${name}.json`,
    sha256: sha,
    bytes: 100,
    contentType: "application/json",
  };
}

function manifest({ asOf = "2026-08-27", releaseId = "release:2026-08-27:aaaaaaaaaaaaaaaa", slot = "dashboard", names = MARKET_READ_MODEL_NAMES } = {}) {
  const artifacts = Object.fromEntries(names.map((name, index) => [name, descriptor(name, asOf, releaseId, SHA(String.fromCharCode(97 + (index % 6))))]));
  return {
    schemaVersion: 3,
    slot,
    releaseId,
    asOf,
    observedAt: "2026-08-27T12:00:00.000Z",
    producerBuildSha: "a".repeat(40),
    sourceDates: { sse_etf: asOf, szse_etf: asOf },
    pressureSchemaVersion: 2,
    stockShardCount: 8,
    requiredArtifacts: names,
    artifacts,
  };
}

function releaseRow({ asOf = "2026-08-27", slot = "dashboard" } = {}) {
  const releaseId = `release:${asOf}:${slot === "dashboard" ? "aaaaaaaaaaaaaaaa" : "bbbbbbbbbbbbbbbb"}`;
  const value = manifest({ asOf, releaseId, slot, names: slot === "dashboard" ? MARKET_READ_MODEL_NAMES : PENETRATION_READ_MODEL_NAMES });
  return {
    release_id: releaseId,
    as_of: asOf,
    status: "ACTIVE",
    schema_version: 3,
    manifest_sha256: SHA("c"),
    manifest_json: JSON.stringify(value),
    observed_at: value.observedAt,
    created_at: value.observedAt,
  };
}

function legacyReleaseRow({ asOf = "2026-08-27" } = {}) {
  const releaseId = `release:${asOf}:cccccccccccccccc`;
  const current = manifest({ asOf, releaseId, slot: "dashboard", names: MARKET_READ_MODEL_NAMES });
  const value = {
    schemaVersion: 2,
    releaseId,
    asOf,
    artifacts: current.artifacts,
  };
  return {
    release_id: releaseId,
    as_of: asOf,
    status: "SUPERSEDED",
    schema_version: 2,
    manifest_sha256: SHA("c"),
    manifest_json: JSON.stringify(value),
    observed_at: null,
    created_at: "2026-08-27T12:00:00.000Z",
  };
}

function objectsFor(row) {
  const value = JSON.parse(row.manifest_json);
  return Object.fromEntries(Object.values(value.artifacts).map((item) => [item.key, { key: item.key, size: item.bytes, customMetadata: { sha256: item.sha256 } }]));
}

function baseFixture() {
  const dashboard = releaseRow({});
  const penetration = releaseRow({ slot: "etf_penetration" });
  const objects = { ...objectsFor(dashboard), ...objectsFor(penetration) };
  const d1 = new FakeD1({
      dates: ["2026-08-27", "2026-08-28", "2026-08-31"],
      releases: [dashboard, penetration],
      historical: [{ as_of: "2026-08-27", observed_at: "2026-08-27T12:00:00.000Z", snapshot_sha256: SHA("d"), r2_object_key: "market-data/history/2026-08-27.json", manifest_key: "market-data/history/2026-08-27.manifest.json", quality_pass: 1, quality_warn: 0, quality_fail: 0 }],
      models: [{ source: "etf", trade_date: "2026-08-27", market: "SSE", row_count: 3, latest_observed_at: "2026-08-27T12:00:00.000Z" }],
      pipelines: [{ run_key: "daily-market-data:2026-08-27:evening:v1", pipeline: "daily-market-data", window_date: "2026-08-27", window_name: "evening", contract_version: "v1", target_date: "2026-08-27", status: "COMPLETED", lease_owner: null, lease_expires_at: null, source_fingerprint: "fingerprint", github_run_id: "123", attempt_count: 1, last_error_code: null, updated_at: "2026-08-27T12:00:00.000Z", completed_at: "2026-08-27T12:00:00.000Z", recovery_id: null }],
      activeReleases: [{ slot: "dashboard", release_id: dashboard.release_id, as_of: "2026-08-27", manifest_sha256: SHA("c"), observed_at: "2026-08-27T12:00:00.000Z", activated_at: "2026-08-27T12:00:00.000Z" }],
    });
  const r2 = new FakeR2(objects);
  return { d1, r2, DB: d1, RAW_BUCKET: r2 };
}

test("query validation enforces dates, keyset cursor and hard limits", () => {
  assert.equal(parseInventoryQuery(new URLSearchParams("start=2026-08-01&end=2026-08-31&limit=3")).limit, 3);
  assert.throws(() => parseInventoryQuery(new URLSearchParams("start=2026-08-31&end=2026-08-01")), /invalid_date_range/);
  assert.throws(() => parseInventoryQuery(new URLSearchParams(`start=2020-01-01&end=2026-01-01`)), /date_range_too_large/);
  assert.throws(() => parseInventoryQuery(new URLSearchParams(`start=2026-08-01&end=2026-08-31&limit=${MAX_INVENTORY_LIMIT + 1}`)), /invalid_limit/);
  const cursor = encodeInventoryCursor("2026-08-31");
  assert.equal(decodeInventoryCursor(cursor), "2026-08-31");
  assert.throws(() => parseInventoryQuery(new URLSearchParams(`start=2026-08-01&end=2026-08-31&cursor=${cursor}`)), /cursor_out_of_range/);
  assert.equal(MAX_DATE_SPAN_DAYS, 1100);
  const r2Cursor = encodeR2InventoryCursor("2026-08-01", "2026-08-31", "market-data/releases/2026-08-27/release:2026-08-27:0000000000000000/artifact.json");
  assert.ok(r2Cursor.length <= MAX_R2_CURSOR_LENGTH);
  assert.equal(decodeR2InventoryCursor(r2Cursor).end, "2026-08-31");
  assert.throws(() => parseInventoryQuery(new URLSearchParams(`start=2026-08-01&end=2026-08-31&r2Cursor=${encodeR2InventoryCursor("2026-08-02", "2026-08-31", "market-data/releases/2026-08-27/release:2026-08-27:0000000000000000/artifact.json")}`)), /r2_cursor_scope_mismatch/);
});

test("admin endpoint authorization accepts only the existing exact bearer key", () => {
  assert.equal(isRecoveryInventoryAuthorized("secret-key", "secret-key"), true);
  assert.equal(isRecoveryInventoryAuthorized("secret-key", "secret-key-2"), false);
  assert.equal(isRecoveryInventoryAuthorized("", "secret-key"), false);
  assert.equal(isRecoveryInventoryAuthorized("secret-key", ""), false);
});

test("same fixture paginates deterministically without duplicate dates and keeps slots independent", async () => {
  const fixture = baseFixture();
  const first = await collectRecoveryInventory(fixture, { start: "2026-08-01", end: "2026-09-03", cursor: null, limit: 2 });
  assert.equal(first.status, "COMPLETE", JSON.stringify(first));
  assert.deepEqual(first.dates.map((item) => item.dataDate), ["2026-08-27", "2026-08-28"]);
  assert.equal(first.hasMore, true);
  assert.equal(first.dates[0].releases.dashboard.length, 1);
  assert.equal(first.dates[0].releases.etf_penetration.length, 1);
  assert.equal(first.dates[0].domains[0].stageStatus.dashboard, "READY", JSON.stringify(first));
  assert.equal(first.dates[0].domains[0].stageStatus.etf_penetration, "READY");
  assert.equal(first.dates[0].domains[0].stageStatus.model, "READY");
  assert.equal(first.dates[0].pipelineWindows[0].status, "COMPLETED");
  assert.equal(first.activeReleaseSlots[0].slot, "dashboard");
  assert.ok(Array.isArray(first.dates[0].sourceReadiness));
  assert.ok(Array.isArray(first.dates[0].artifactPointers));
  const second = await collectRecoveryInventory(fixture, { start: "2026-08-01", end: "2026-09-03", cursor: "2026-08-28", limit: 2 });
  assert.deepEqual(second.dates.map((item) => item.dataDate), ["2026-08-31"]);
  assert.equal(second.hasMore, false);
  assert.equal(new Set([...first.dates, ...second.dates].map((item) => item.dataDate)).size, 3);
  assert.equal(first.usage.d1.rowsWritten, 0);
  assert.ok(fixture.r2.heads.length > 0);
  assert.ok(fixture.r2.heads.every((key) => key.startsWith("market-data/releases/")));
  assert.ok(fixture.d1.calls.every(({ sql }) => /LIMIT\s+\?/i.test(sql)));
  assert.ok(fixture.d1.calls.some(({ sql }) => /FROM\s+etf_daily/i.test(sql)));
});

test("legacy schema v2 slotless market manifests remain inventory-compatible", async () => {
  const legacy = legacyReleaseRow({});
  const fixture = new FakeD1({ dates: ["2026-08-27"], releases: [legacy] });
  const r2 = new FakeR2(objectsFor(legacy));
  const result = await collectRecoveryInventory(
    { DB: fixture, RAW_BUCKET: r2 },
    { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 },
  );
  assert.equal(result.status, "COMPLETE", JSON.stringify(result));
  assert.equal(result.incompleteReasons.includes("RELEASE_MANIFEST_INCOMPLETE"), false);
  assert.equal(result.dates[0].releases.dashboard[0].manifestComplete, true);
  assert.equal(result.dates[0].domains[0].stageStatus.dashboard, "READY");
});

test("R2 continuation pages avoid repeating unrelated D1 history reads", async () => {
  const fixture = baseFixture();
  const firstManifest = JSON.parse(fixture.d1.fixture.releases[0].manifest_json);
  const key = Object.values(firstManifest.artifacts)[0].key;
  const token = encodeR2InventoryCursor("2026-08-01", "2026-09-03", key);
  const result = await collectRecoveryInventory(fixture, {
    start: "2026-08-01",
    end: "2026-09-03",
    cursor: null,
    limit: 2,
    r2Cursor: token,
    r2CursorKey: key,
  });
  assert.equal(result.request.r2Cursor, token);
  assert.ok(fixture.d1.calls.some(({ sql }) => /WITH dates/i.test(sql)));
  assert.ok(fixture.d1.calls.some(({ sql }) => /FROM release_manifest/i.test(sql)));
  assert.equal(fixture.d1.calls.length, 2);
  assert.ok(fixture.d1.calls.filter(({ sql }) => !/WITH dates/i.test(sql)).every(({ sql }) => !/FROM (historical_snapshot|dashboard_snapshot|source_readiness|history_artifact_pointer|etf_daily|margin_daily|connect_daily|pipeline_window|active_release)/i.test(sql)));
});

test("pipeline and recovery reads stay below D1 bind cap on a full date page", async () => {
  const fixture = baseFixture();
  fixture.d1.fixture.dates = Array.from({ length: 100 }, (_, index) => {
    const value = new Date(Date.UTC(2024, 7, 1 + index));
    return value.toISOString().slice(0, 10);
  });
  const result = await collectRecoveryInventory(fixture, {
    start: "2024-08-01",
    end: "2026-09-03",
    cursor: null,
    limit: 100,
  });
  assert.equal(result.status, "COMPLETE", JSON.stringify(result));
  assert.ok(fixture.d1.calls.length > 0);
  assert.ok(fixture.d1.calls.every(({ values }) => values.length <= MAX_D1_BOUND_PARAMETERS));
  assert.equal(
    fixture.d1.calls.find(({ sql }) => /FROM pipeline_window\s/i.test(sql)).values.length,
    4,
  );
  assert.equal(
    fixture.d1.calls.find(({ sql }) => /FROM pipeline_window_recovery\s/i.test(sql)).values.length,
    3,
  );
});

test("R2 hash mismatch and failed HEAD remain INCOMPLETE, never READY", async () => {
  const fixture = baseFixture();
  const dashboard = JSON.parse(fixture.d1.fixture.releases[0].manifest_json);
  fixture.r2.objects[dashboard.artifacts.bootstrap.key].customMetadata.sha256 = SHA("z");
  const mismatch = await collectRecoveryInventory(fixture, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(mismatch.status, "INCOMPLETE");
  assert.ok(mismatch.incompleteReasons.includes("R2_HASH_MISMATCH"));
  assert.notEqual(mismatch.dates[0].releases.dashboard[0].artifacts.bootstrap.status, "READY");
  const failed = baseFixture();
  failed.r2.failure = "403 forbidden";
  const unavailable = await collectRecoveryInventory(failed, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(unavailable.status, "INCOMPLETE");
  assert.ok(unavailable.incompleteReasons.includes("R2_HEAD_FORBIDDEN"));
});

test("a release manifest without its declared hash cannot make a slot READY", async () => {
  const fixture = baseFixture();
  fixture.d1.fixture.releases[0].manifest_sha256 = null;
  const result = await collectRecoveryInventory(fixture, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("RELEASE_MANIFEST_HASH_MISSING"));
  assert.notEqual(result.dates[0].domains.find((item) => item.domain === "sse_etf").stageStatus.dashboard, "READY");
});

test("D1 write telemetry violation or missing telemetry fails closed", async () => {
  const violation = baseFixture();
  violation.d1.rowsWritten = 1;
  const result = await collectRecoveryInventory(violation, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("D1_READONLY_VIOLATION"));
  const missing = baseFixture();
  missing.d1.missingMeta = true;
  const unknown = await collectRecoveryInventory(missing, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(unknown.status, "INCOMPLETE");
  assert.equal(unknown.usage.d1.rowsWrittenKnown, false);
  assert.ok(unknown.incompleteReasons.includes("D1_ROWS_WRITTEN_TELEMETRY_UNKNOWN"));
});

test("D1 row-read hard cap fails closed before the bounded page can look complete", async () => {
  const overBudget = baseFixture();
  overBudget.d1.rowsRead = MAX_D1_ROWS_READ;
  const result = await collectRecoveryInventory(overBudget, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("D1_ROWS_READ_LIMIT"));
  assert.equal(result.usage.d1.rowsRead, MAX_D1_ROWS_READ);
  assert.equal(overBudget.d1.calls.length, 1);
});

test("mid-page D1 failure returns incomplete and does not claim an empty complete page", async () => {
  const fixture = baseFixture();
  fixture.d1.failOn = "SELECT release_id";
  const result = await collectRecoveryInventory(fixture, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("D1_OR_R2_READ_FAILED"));
});

test("an empty observed-date page is incomplete rather than a false READY inventory", async () => {
  const empty = { DB: new FakeD1({ dates: [] }), RAW_BUCKET: new FakeR2() };
  const result = await collectRecoveryInventory(empty, { start: "2026-09-04", end: "2026-09-04", cursor: null, limit: 1 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("NO_OBSERVED_DATES"));
});

test("source-unavailable requires provenance and stages stay separate", async () => {
  const fixture = baseFixture();
  fixture.d1.fixture.readiness = [{
    target_date: "2026-08-27",
    source_id: "sse_etf",
    status: "error",
    required: 1,
    observed_at: "2026-08-27T12:00:00.000Z",
    deadline_at: "2026-08-27T13:00:00.000Z",
    deadline_exceeded: 1,
    observed_dates_json: "[]",
    watermark_json: "{}",
    reason: "official endpoint unavailable",
    evidence_json: JSON.stringify({ sourceFingerprint: "sse:2026-08-27:unavailable", officialUrl: "https://example.invalid", apiToken: "must-not-leak" }),
    updated_at: "2026-08-27T12:00:00.000Z",
  }];
  const result = await collectRecoveryInventory(fixture, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  const sse = result.dates[0].domains.find((item) => item.domain === "sse_etf");
  assert.equal(sse.stageStatus.raw, "SOURCE_UNAVAILABLE");
  assert.equal(sse.evidence.raw[0].evidence.apiToken, undefined);
  assert.equal(sse.stageStatus.dashboard, "READY");
  assert.equal(sse.stageStatus.etf_penetration, "READY");
  const incomplete = baseFixture();
  incomplete.d1.fixture.readiness = [{ ...fixture.d1.fixture.readiness[0], evidence_json: "{}" }];
  const second = await collectRecoveryInventory(incomplete, { start: "2026-08-27", end: "2026-08-27", cursor: null, limit: 1 });
  assert.equal(second.dates[0].domains.find((item) => item.domain === "sse_etf").stageStatus.raw, "UNKNOWN");
});

test("hard caps are visible and source contains no R2 list/get or write paths", async () => {
  assert.equal(MAX_R2_HEADS, 64);
  assert.equal(MAX_D1_ROWS_READ, 500_000);
  assert.deepEqual(INVENTORY_STAGES, ["raw", "normalized", "model", "pcf_exact", "dashboard", "etf_penetration", "date_visible"]);
  const [library, route, schema] = await Promise.all([
    readFile(new URL("../app/lib/recovery-inventory.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/v1/admin/recovery-inventory/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(library, /(?:bucket|RAW_BUCKET|runtime\.RAW_BUCKET)\.list\s*\(/);
  assert.doesNotMatch(library, /(?:bucket|RAW_BUCKET|runtime\.RAW_BUCKET)\.get\s*\(/);
  assert.doesNotMatch(route, /(?:bucket|RAW_BUCKET|runtime\.RAW_BUCKET)\.list\s*\(|(?:bucket|RAW_BUCKET|runtime\.RAW_BUCKET)\.get\s*\(/);
  assert.doesNotMatch(library, /INSERT\s+INTO|UPDATE\s+|DELETE\s+|DROP\s+/i);
  assert.doesNotMatch(library, /fetch\s*\(|workflow_dispatch|dispatch/i);
  assert.doesNotMatch(route, /fetch\s*\(|workflow_dispatch|dispatch/i);
  assert.match(library, /ORDER BY data_date ASC/);
  assert.doesNotMatch(library, /OFFSET\s+\?/i);
  for (const table of ["historical_snapshot", "release_manifest", "dashboard_snapshot", "market_history_daily", "source_readiness", "history_artifact_pointer", "etf_daily", "margin_daily", "connect_daily"]) {
    assert.match(library, new RegExp(`FROM ${table}`));
  }
  for (const index of [
    "release_manifest_as_of_idx",
    "dashboard_snapshot_as_of_idx",
    "source_readiness_target_idx",
    "history_artifact_pointer_date_idx",
    "etf_daily_lookup_idx",
    "margin_daily_lookup_idx",
    "connect_daily_lookup_idx",
  ]) {
    assert.match(schema, new RegExp(index));
  }
  assert.match(library, /FROM pipeline_window[\s\S]*WHERE run_key >= \? AND run_key < \?/);
  assert.match(library, /FROM pipeline_window_recovery[\s\S]*WHERE run_key >= \? AND run_key < \?/);
  assert.doesNotMatch(library, /SELECT\s+\*/i);
});

test("R2 head work is bounded even when many release artifacts are present", async () => {
  const fixture = baseFixture();
  const rows = [];
  for (let index = 0; index < 6; index += 1) {
    const asOf = `2026-08-${String(20 + index).padStart(2, "0")}`;
    for (const slot of ["dashboard", "etf_penetration"]) {
      const row = releaseRow({ asOf, slot });
      rows.push(row);
      Object.assign(fixture.r2.objects, objectsFor(row));
    }
  }
  fixture.d1.fixture.dates = rows.map((row) => row.as_of);
  fixture.d1.fixture.releases = rows;
  const result = await collectRecoveryInventory(fixture, { start: "2026-08-20", end: "2026-08-25", cursor: null, limit: 6 });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("R2_HEAD_PAGE_REMAINS"));
  assert.ok(fixture.r2.heads.length <= MAX_R2_HEADS);
});

test("R2 continuation cursor must belong to the current candidate index", async () => {
  const fixture = baseFixture();
  const key = "market-data/releases/2026-08-27/release:2026-08-27:ffffffffffffffff/not-present.json";
  const token = encodeR2InventoryCursor("2026-08-01", "2026-09-03", key);
  const result = await collectRecoveryInventory(fixture, {
    start: "2026-08-01",
    end: "2026-09-03",
    cursor: null,
    limit: 2,
    r2Cursor: token,
    r2CursorKey: key,
  });
  assert.equal(result.status, "INCOMPLETE");
  assert.ok(result.incompleteReasons.includes("R2_CURSOR_NOT_IN_INDEX"));
});
