import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash, webcrypto } from "node:crypto";
import { DIMENSIONS, BUDGET_SCOPE, MAX_RECEIPTS, MAX_RESERVATIONS, MAX_STATE_BYTES, createSharedBudgetState, transitionSharedBudget, sharedBudgetRequest } from "../app/lib/pipeline-shared-budget.ts";
import { canonicalJson } from "../app/lib/pipeline-recovery-plan.ts";
globalThis.crypto ??= webcrypto;
const now = new Date("2026-10-03T02:00:00.000Z");
const units = ["rows", "rows", "requests", "minutes", "bytes", "operations", "operations"];
const vec = (value, measured = false) => Object.fromEntries(DIMENSIONS.map((d, i) => [d, { value, unit: units[i], basis: measured ? "measured" : "estimated", ...(measured ? { measuredAt: now.toISOString(), scope: "account" } : {}) }]));
const nums = value => Object.fromEntries(DIMENSIONS.map(d => [d, value]));
const state = () => createSharedBudgetState({ epochDay: "2026-10-03", epochMonth: "2026-10", evidence: vec(0, true), caps: nums(100), targets: nums(90), measurementTtlSeconds: 3600, reservationTtlSeconds: 1800 });
const key = i => `daily-market-data:2026-10-03:${i % 2 ? "morning" : "reconcile"}:v1`;
const input = (id = 0, operation = "reserve", value = 70) => ({ runKey: key(id), leaseOwner: `owner${id}`, attemptCount: 1, targetDate: "2026-10-02", operationId: createHash("sha256").update(`${id}/${operation}/${value}`).digest("hex"), operation, expectedRevision: 0, vector: vec(value) });
function seed(path = ":memory:") {
  const db = new DatabaseSync(path);
  db.exec(readFileSync(new URL("../drizzle/0015_shared_budget_account.sql", import.meta.url), "utf8"));
  db.exec("CREATE TABLE pipeline_window (run_key TEXT,lease_owner TEXT,attempt_count INTEGER,target_date TEXT,recovery_id TEXT,status TEXT,lease_expires_at TEXT,PRIMARY KEY(run_key,lease_owner));");
  db.prepare("INSERT INTO pipeline_budget_account VALUES (?,0,?,?)").run(BUDGET_SCOPE, JSON.stringify(state()), now.toISOString());
  for (let i = 0; i < 20; i++) db.prepare("INSERT INTO pipeline_window VALUES (?,?,1,'2026-10-02',NULL,'RUNNING','2026-10-03T03:00:00Z')").run(key(i), `owner${i}`);
  return db;
}
// Real local SQLite SQL/CAS, not physical D1 telemetry. Deliberate synthetic meta
// proves propagation; production meta remains provider-only runtime evidence.
function adapter(sqlite, options = {}) {
  return { withSession(c) { assert.equal(c, "first-primary"); return this; }, prepare(sql) {
    assert(!/DROP|DELETE|INSERT|REPLACE/.test(sql));
    return { bind(...args) { return { async all() {
      options.before?.(sql);
      if (options.fail) throw new Error("fixture db failure");
      const rows = sqlite.prepare(sql).all(...args);
      return { results: rows, meta: options.unknownMeta ? {} : { rows_read: 7, rows_written: sql.startsWith("UPDATE") ? 1 : 0 } };
    } }; } };
  } };
}
const request = (payload, token = "fixture-key") => new Request("https://flow.xiaoker.com/api/v1/admin/pipeline-window/budget", { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: JSON.stringify(payload) });
const call = (db, p, opts) => sharedBudgetRequest(request(p), { DB: adapter(db, opts), INGEST_API_KEY: "fixture-key" }, () => now, nums(100));
function readState(db) { return JSON.parse(db.prepare("SELECT state_json FROM pipeline_budget_account WHERE scope=?").get(BUDGET_SCOPE).state_json); }
function recoveryFixture(manifest = { runnerBudgetContract: "flow-shared-budget-v1", taskCostUpperBound: vec(80) }) {
  const db = seed();
  const plan = { schemaVersion: 1, contractVersion: "recovery-execution-v1", campaignId: "fixture", recoveryId: "fixture-recovery",
    runKey: key(0), dataDates: ["2026-10-02"], requiredStages: ["core_collection"], artifactRoot: ".recovery/fixture",
    stateDir: ".recovery/fixture/state", maxDates: 1, maxRequests: 20, deadline: "2026-10-03T03:00:00Z", budgetManifest: manifest };
  plan.planHash = createHash("sha256").update(canonicalJson(plan)).digest("hex");
  const bytes = new TextEncoder().encode(canonicalJson(plan) + "\n");
  const scopeSha256 = createHash("sha256").update(bytes).digest("hex");
  const objectKey = `market-data/recovery/plans/${plan.recoveryId}-${scopeSha256}.json`;
  db.exec("CREATE TABLE pipeline_window_recovery (recovery_id TEXT PRIMARY KEY,run_key TEXT,plan_key TEXT,plan_sha256 TEXT,scope_sha256 TEXT,status TEXT,expires_at TEXT)");
  db.prepare("INSERT INTO pipeline_window_recovery VALUES (?,?,?,?,?,'ACTIVE','2026-10-03T03:00:00Z')").run(plan.recoveryId, plan.runKey, objectKey, plan.planHash, scopeSha256);
  db.prepare("UPDATE pipeline_window SET recovery_id=? WHERE run_key=? AND lease_owner=?").run(plan.recoveryId, key(0), "owner0");
  const counts = { head: 0, get: 0 };
  const bucket = { async head(k) { counts.head++; assert.equal(k, objectKey); return { size: bytes.length, customMetadata: { sha256: scopeSha256 } }; },
    async get(k) { counts.get++; assert.equal(k, objectKey); return { arrayBuffer: async () => bytes.buffer }; }, async put() { assert.fail("no plan writes"); } };
  const runtime = { DB: adapter(db), INGEST_API_KEY: "fixture-key", PIPELINE_RECOVERY_ENABLED: "1", RAW_BUCKET: bucket };
  const p = { ...input(), recoveryId: plan.recoveryId, planSha256: plan.planHash, scopeSha256 };
  const run = (payload = p, overrides = {}) => sharedBudgetRequest(request(payload), { ...runtime, ...overrides }, () => now, nums(100));
  return { db, plan, p, runtime, run, counts };
}

test("recovery uses exact approved plan and the same account CAS, not another quota", async () => {
  const f = recoveryFixture();
  assert.equal((await (await f.run()).json()).decision, "allow");
  assert.equal(f.counts.head, 1); assert.equal(f.counts.get, 1);
  const id = `recovery/${f.p.recoveryId}/${f.p.planSha256}/${key(0)}/owner0/1`;
  assert.equal(readState(f.db).reservations[id].cost.d1RowsRead, 70);
  const replay = await (await f.run()).json(); assert.equal(replay.replayed, true);
  assert.equal(f.db.prepare("SELECT revision FROM pipeline_budget_account").get().revision, 1);
  const ordinary = await call(f.db, { ...input(1), vector: vec(40), expectedRevision: 1 });
  assert.equal((await ordinary.json()).decision, "over_budget");
  f.db.close();
});

test("recovery flag, ID/hash/date/expiry and missing approved bound fail closed", async () => {
  for (const mutate of [{ recoveryId: undefined }, { planSha256: "0".repeat(64) }, { scopeSha256: "0".repeat(64) },
    { recoveryId: "other" }, { targetDate: "2026-10-03" }, { attemptCount: 2 }, { leaseOwner: "other" }]) {
    const f = recoveryFixture(); assert.notEqual((await f.run({ ...f.p, ...mutate })).status, 200);
    assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
  }
  for (const flag of ["0", undefined]) {
    const f = recoveryFixture(); assert.equal((await f.run(f.p, { PIPELINE_RECOVERY_ENABLED: flag })).status, 404);
    assert.equal(f.counts.head, 0); f.db.close();
  }
  for (const change of ["status='REVOKED'", "status='PAUSED'", "expires_at='2026-10-03T01:59:00Z'"]) {
    const f = recoveryFixture(); f.db.prepare(`UPDATE pipeline_window_recovery SET ${change}`).run();
    assert.equal((await f.run()).status, 409); assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
  }
  for (const manifest of [{ fixture: true }, "ops-budget/fixture", { runnerBudgetContract: "flow-shared-budget-v1", taskCostUpperBound: { ...vec(80), r2Bytes: { unit: "bytes", value: null, basis: "unknown" } } }]) {
    const f = recoveryFixture(manifest); assert.notEqual((await f.run()).status, 200);
    assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
  }
});

test("revocation between preflight and commit is atomically fenced", async () => {
  const f = recoveryFixture();
  const db = adapter(f.db, { before(sql) { if (sql.startsWith("UPDATE pipeline_budget_account")) f.db.prepare("UPDATE pipeline_window_recovery SET status='REVOKED'").run(); } });
  assert.equal((await f.run(f.p, { DB: db })).status, 409);
  assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
});

test("takeover cannot multiply the plan budget; settlements retain conservative scope charge", async () => {
  const f = recoveryFixture();
  assert.equal((await (await f.run()).json()).decision, "allow");
  const settle = { ...f.p, operation: "settle", operationId: "b".repeat(64), expectedRevision: 1, outcome: "SUCCEEDED", vector: vec(10, true) };
  assert.equal((await (await f.run(settle)).json()).decision, "settled");
  f.db.prepare("UPDATE pipeline_window SET lease_owner='next',attempt_count=2 WHERE run_key=? AND lease_owner='owner0'").run(key(0));
  const next = { ...f.p, leaseOwner: "next", attemptCount: 2, operationId: "c".repeat(64), expectedRevision: 2, vector: vec(20) };
  assert.equal((await f.run(next)).status, 409);
  assert.equal(Object.keys(readState(f.db).reservations).length, 1); f.db.close();
});

test("invalid immutable plan and UNKNOWN account never grant recovery allowance", async () => {
  const f = recoveryFixture();
  const corrupted = { ...f.runtime.RAW_BUCKET, async get() { return { arrayBuffer: async () => new TextEncoder().encode("{}").buffer }; } };
  assert.equal((await f.run(f.p, { RAW_BUCKET: corrupted })).status, 409);
  const s = readState(f.db); s.evidence.r2Bytes = { value: null, unit: "bytes", basis: "unknown" };
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=?").run(JSON.stringify(s));
  assert.equal((await (await f.run()).json()).decision, "unknown_metrics");
  assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
});

test("recovery and ordinary contenders cannot oversell the shared account", async () => {
  const f = recoveryFixture();
  const responses = await Promise.all([f.run(), ...Array.from({ length: 19 }, (_, i) => call(f.db, input(i + 1)))]);
  const results = await Promise.all(responses.map(r => r.json()));
  assert.equal(results.filter(r => r.decision === "allow").length, 1);
  assert.equal(Object.keys(readState(f.db).reservations).length, 1);
  assert.equal(f.db.prepare("SELECT revision FROM pipeline_budget_account").get().revision, 1);
  f.db.close();
});

test("recovery approval query uses its primary key and plan expiry cannot be extended by lease", async () => {
  const f = recoveryFixture();
  const explanation = f.db.prepare("EXPLAIN QUERY PLAN SELECT plan_key FROM pipeline_window_recovery WHERE recovery_id=? AND run_key=? AND plan_sha256=? AND scope_sha256=? LIMIT 1")
    .all(f.p.recoveryId, f.p.runKey, f.p.planSha256, f.p.scopeSha256);
  assert.match(explanation[0].detail, /INDEX/);
  // The immutable plan is expired although both database expiries are valid.
  const expired = structuredClone(f.plan); delete expired.planHash; expired.deadline = "2026-10-03T01:59:00Z";
  expired.planHash = createHash("sha256").update(canonicalJson(expired)).digest("hex");
  const bytes = new TextEncoder().encode(canonicalJson(expired) + "\n");
  const sha = createHash("sha256").update(bytes).digest("hex");
  const objectKey = `market-data/recovery/plans/${expired.recoveryId}-${sha}.json`;
  f.db.prepare("UPDATE pipeline_window_recovery SET plan_key=?,plan_sha256=?,scope_sha256=?").run(objectKey, expired.planHash, sha);
  const bucket = { async head() { return { size: bytes.length, customMetadata: { sha256: sha } }; },
    async get() { return { arrayBuffer: async () => bytes.buffer }; }, async put() { assert.fail(); } };
  assert.equal((await f.run({ ...f.p, planSha256: expired.planHash, scopeSha256: sha }, { RAW_BUCKET: bucket })).status, 409);
  assert.deepEqual(readState(f.db).reservations, {}); f.db.close();
});
test("additive migration, metadata and indexed bounded queries", () => {
  const db = seed();
  const sql = readFileSync(new URL("../drizzle/0015_shared_budget_account.sql", import.meta.url), "utf8");
  assert(!/DROP|DELETE|ALTER|INSERT/i.test(sql));
  assert.match(db.prepare("EXPLAIN QUERY PLAN SELECT revision FROM pipeline_budget_account WHERE scope=? LIMIT 1").all(BUDGET_SCOPE)[0].detail, /INDEX/);
  const journal = JSON.parse(readFileSync(new URL("../drizzle/meta/_journal.json", import.meta.url)));
  assert.equal(journal.entries.at(-1).tag, "0015_shared_budget_account");
  const snapshot = JSON.parse(readFileSync(new URL("../drizzle/meta/0015_snapshot.json", import.meta.url)));
  assert(snapshot.tables.pipeline_budget_account); db.close();
});
test("same parity fixture executes through server transitions", () => {
  const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/shared-budget-trace.json", import.meta.url)));
  let s = state();
  for (const step of fixture.trace) {
    const next = transitionSharedBudget(s, "task", { ...step, ...(step.value === undefined ? {} : { vector: vec(step.value, step.measured) }) }, now);
    assert.equal(next.result.decision, step.decision); assert.equal(next.result.status, step.status); s = next.state;
  }
  assert.deepEqual(s.settled, nums(8)); assert.deepEqual(s.reservations.task.authorized, nums(10));
});
test("20 independent clients race real SQLite CAS; only one grants reservation", async () => {
  const dir = mkdtempSync(join(tmpdir(), "flow-budget-")); const path = join(dir, "state.sqlite");
  const first = seed(path); const clients = Array.from({ length: 20 }, () => new DatabaseSync(path));
  try {
    const responses = await Promise.all(clients.map((db, i) => call(db, input(i))));
    const receipts = await Promise.all(responses.map(r => r.json()));
    assert.equal(receipts.filter(r => r.decision === "allow").length, 1);
    assert.equal(responses.filter(r => r.status === 409).length, 19);
    assert.equal(Object.keys(readState(first).reservations).length, 1);
    const revision = first.prepare("SELECT revision FROM pipeline_budget_account").get().revision;
    const loser = receipts.findIndex(r => r.error);
    const retry = await call(clients[loser], { ...input(loser), expectedRevision: revision });
    assert.equal((await retry.json()).decision, "over_budget");
  } finally { first.close(); clients.forEach(db => db.close()); rmSync(dir, { recursive: true }); }
});
test("lost reply same-ID replay never spends twice; mutated receipt conflicts", async () => {
  const db = seed(); const p = input();
  assert.equal((await call(db, p)).status, 200);
  const replay = await (await call(db, p)).json(); assert.equal(replay.replayed, true);
  assert.equal(db.prepare("SELECT revision FROM pipeline_budget_account").get().revision, 1);
  assert.equal((await call(db, { ...p, vector: vec(71) })).status, 409);
  const grant = { ...input(0, "authorize_step", 10), expectedRevision: 1 };
  assert.equal((await (await call(db, grant)).json()).decision, "authorized");
  const repeatedGrant = await (await call(db, grant)).json();
  assert.equal(repeatedGrant.replayed, true); assert.equal(repeatedGrant.decision, "not_authorized");
  assert.deepEqual(Object.values(readState(db).reservations)[0].authorized, nums(10)); db.close();
});
test("auth, identity, lease, state and metadata fail closed", async () => {
  const db = seed(); let queries = 0;
  const runtime = { DB: adapter(db, { before: () => queries++ }), INGEST_API_KEY: "fixture-key" };
  assert.equal((await sharedBudgetRequest(request(input(), "wrong"), runtime)).status, 401); assert.equal(queries, 0);
  for (const mutation of [{ leaseOwner: undefined }, { targetDate: "2026-10-01" }, { attemptCount: 2 }, { runKey: "invalid" }]) assert.notEqual((await call(db, { ...input(), ...mutation })).status, 200);
  db.prepare("UPDATE pipeline_window SET lease_expires_at='2026-10-03T01:59:00Z'").run();
  assert.equal((await call(db, input())).status, 409);
  assert.deepEqual(readState(db).reservations, {});
  db.prepare("UPDATE pipeline_window SET lease_expires_at='2026-10-03T03:00:00Z'").run();
  const unknown = await (await call(db, input(), { unknownMeta: true })).json();
  assert.equal(unknown.rowsRead, null); assert.equal(unknown.rowsWritten, null); db.close();
});
test("atomic mutation rechecks takeover and handles storage failure without allowance", async () => {
  for (const fail of [false, true]) {
    const db = seed();
    const options = fail ? { fail: true } : { before(sql) { if (sql.startsWith("UPDATE")) db.prepare("UPDATE pipeline_window SET attempt_count=2").run(); } };
    const response = await call(db, input(), options);
    assert.notEqual(response.status, 200); assert.deepEqual(readState(db).reservations, {}); db.close();
  }
});
test("mutation-stage SQL failure and every invalid lease state preserve the account", async () => {
  const db = seed();
  const response = await call(db, input(), { before(sql) { if (sql.startsWith("UPDATE")) throw new Error("commit unavailable"); } });
  assert.equal(response.status, 503); assert.deepEqual(readState(db).reservations, {});
  for (const change of ["status='COMPLETED'", "status='BLOCKED_QUALITY'", "recovery_id='recovery-fixture'"] ) {
    db.prepare(`UPDATE pipeline_window SET status='RUNNING',recovery_id=NULL,${change}`).run();
    assert.equal((await call(db, input())).status, 409);
  }
  assert.deepEqual(readState(db).reservations, {}); db.close();
});
test("reservation and serialized state limits never clear prior holds", () => {
  let s = state();
  for (let i = 0; i < MAX_RESERVATIONS; i++) s = transitionSharedBudget(s, `task${i}`, { operation: "reserve", vector: vec(0) }, now).state;
  assert.throws(() => transitionSharedBudget(s, "extra", { operation: "reserve", vector: vec(0) }, now), /capacity/);
  assert.equal(Object.keys(s.reservations).length, MAX_RESERVATIONS);
  const large = state(); large.evidence.r2Bytes.scope = "x".repeat(MAX_STATE_BYTES);
  assert.throws(() => transitionSharedBudget(large, "task", { operation: "reserve", vector: vec(0) }, now), /corrupt/);
});
test("unknown, expired, overflow and stale evidence cannot free holds or admit", () => {
  let s = transitionSharedBudget(state(), "a", { operation: "reserve", vector: vec(70) }, now).state;
  s = transitionSharedBudget(s, "a", { operation: "settle", outcome: "TIMEOUT" }, now).state;
  assert.equal(transitionSharedBudget(s, "b", { operation: "reserve", vector: vec(40) }, now).result.decision, "over_budget");
  const expired = transitionSharedBudget(s, "a", { operation: "authorize_step", vector: vec(1) }, new Date(now.getTime() + 1800001));
  assert.equal(expired.result.decision, "not_authorized"); assert.equal(expired.state.reservations.a.status, "UNKNOWN");
  for (const alter of [s => s.caps.r2ClassA = null, s => s.evidence.d1RowsRead.basis = "estimated", s => s.evidence.r2Bytes.measuredAt = "2026-10-01T00:00:00Z", s => s.epochDay = "2026-10-02"]) {
    const s = state(); alter(s); assert.equal(transitionSharedBudget(s, "a", { operation: "reserve", vector: vec(1) }, now).result.decision, "unknown_metrics");
  }
  s = transitionSharedBudget(state(), "a", { operation: "reserve", vector: vec(30) }, now).state;
  s = transitionSharedBudget(s, "a", { operation: "debit", vector: vec(8, true) }, now).state;
  assert.throws(() => transitionSharedBudget(s, "a", { operation: "settle", outcome: "SUCCEEDED", vector: vec(7, true) }, now), /below_observed/);
  assert.throws(() => transitionSharedBudget(s, "a", { operation: "debit", vector: vec(1) }, now), /requires_measured/);
  s = transitionSharedBudget(s, "a", { operation: "debit", vector: vec(40, true) }, now).state;
  assert.equal(transitionSharedBudget(s, "b", { operation: "reserve", vector: vec(1) }, now).result.decision, "over_budget");
});
test("no auto-initialize, no period reset, bounded receipts and body", async () => {
  const db = seed();
  db.prepare("DELETE FROM pipeline_budget_account").run(); // Fixture-only setup, never runtime.
  assert.equal((await call(db, input())).status, 409);
  const s = state(); for (let i = 0; i < MAX_RECEIPTS; i++) s.receipts[i] = { hash: "a".repeat(64), result: { decision: "allow" } };
  db.prepare("INSERT INTO pipeline_budget_account VALUES (?,0,?,?)").run(BUDGET_SCOPE, JSON.stringify(s), now.toISOString());
  assert.equal((await call(db, input())).status, 409);
  const huge = { ...input(), junk: "x".repeat(20000) };
  assert.equal((await call(db, huge)).status, 413); db.close();
});
test("actual repository policy keeps missing R2 caps UNKNOWN and cannot be raised by a row", async () => {
  const db = seed();
  const response = await sharedBudgetRequest(request(input()), { DB: adapter(db), INGEST_API_KEY: "fixture-key" }, () => now);
  assert.equal((await response.json()).decision, "unknown_metrics");
  assert.deepEqual(readState(db).reservations, {});
  const s = state(); s.caps = nums(1000000); db.prepare("UPDATE pipeline_budget_account SET state_json=?").run(JSON.stringify(s));
  const restricted = await sharedBudgetRequest(request({ ...input(), expectedRevision: 1 }), { DB: adapter(db), INGEST_API_KEY: "fixture-key" }, () => now, nums(60));
  assert.equal((await restricted.json()).decision, "over_budget"); db.close();
});
