import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { canonicalJson } from "../app/lib/pipeline-recovery-plan.ts";
import { checkpointObjectRequest } from "../app/lib/pipeline-checkpoint-object.ts";

const now = () => new Date("2026-10-03T02:00:00Z");
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function fixture({ stages = ["core_collection"], deadline = "2026-10-03T03:00:00Z" } = {}) {
  const plan = { schemaVersion: 1, contractVersion: "recovery-execution-v1", campaignId: "fixture",
    recoveryId: "fixture-recovery", runKey: "daily-market-data:2026-10-03:reconcile:v1",
    dataDates: ["2026-10-02"], requiredStages: stages, artifactRoot: ".recovery/fixture",
    stateDir: ".recovery/fixture/state", maxDates: 1, maxRequests: 20, deadline, budgetManifest: { fixture: true } };
  plan.planHash = hash(canonicalJson(plan));
  const planBytes = new TextEncoder().encode(canonicalJson(plan) + "\n");
  const scope = hash(planBytes);
  const planKey = `market-data/recovery/plans/${plan.recoveryId}-${scope}.json`;
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE pipeline_window (run_key TEXT PRIMARY KEY, lease_owner TEXT, attempt_count INTEGER, target_date TEXT, recovery_id TEXT, status TEXT, lease_expires_at TEXT)");
  db.exec("CREATE TABLE pipeline_window_recovery (recovery_id TEXT PRIMARY KEY, run_key TEXT, plan_key TEXT, plan_sha256 TEXT, scope_sha256 TEXT, status TEXT, expires_at TEXT)");
  db.prepare("INSERT INTO pipeline_window VALUES (?,'owner',1,'2026-10-02',?,'RUNNING','2026-10-03T03:00:00Z')").run(plan.runKey, plan.recoveryId);
  db.prepare("INSERT INTO pipeline_window_recovery VALUES (?,?,?,?,?,'ACTIVE','2026-10-03T03:00:00Z')").run(plan.recoveryId, plan.runKey, planKey, plan.planHash, scope);
  const sqls = [], calls = [], objects = new Map();
  const adapter = { withSession(value) { assert.equal(value, "first-primary"); return this; }, prepare(sql) {
    sqls.push(sql); assert.doesNotMatch(sql, /INSERT|UPDATE|DELETE|DROP/);
    assert.match(sql, /LIMIT 1/);
    return { bind(...args) { return { async first() { return db.prepare(sql).get(...args) ?? null; } }; } };
  } };
  const bucket = {
    async head(key) { calls.push("head"); assert.equal(key, planKey); return { size: planBytes.length, customMetadata: { sha256: scope } }; },
    async get(key) {
      calls.push(key === planKey ? "plan-get" : "object-get");
      if (key === planKey) return { arrayBuffer: async () => planBytes.buffer };
      const o = objects.get(key); if (!o) return null;
      return { size: o.bytes.length, customMetadata: o.metadata,
        body: new ReadableStream({ start(c) { c.enqueue(o.bytes); c.close(); } }) };
    },
    async put(key, bytes, options) {
      calls.push("put"); assert.equal(options.onlyIf.get("If-None-Match"), "*");
      if (objects.has(key)) return null;
      objects.set(key, { bytes: bytes.slice(), metadata: options.customMetadata }); return {};
    },
  };
  const bytes = new TextEncoder().encode('{"fixture":"checkpoint"}');
  const query = new URLSearchParams({ runKey: plan.runKey, targetDate: "2026-10-02", leaseOwner: "owner", attemptCount: "1",
    stage: "core_collection", inputSha256: "b".repeat(64), sha256: hash(bytes), producerBuildSha: "a".repeat(40),
    recoveryId: plan.recoveryId, planSha256: plan.planHash, scopeSha256: scope });
  const request = (method = "POST", token = "fixture-key") => new Request(`https://flow.xiaoker.com/api/v1/admin/pipeline-window/checkpoint?${query}`,
    { method, headers: { Authorization: `Bearer ${token}` }, ...(method === "POST" ? { body: bytes } : {}) });
  const runtime = { DB: adapter, RAW_BUCKET: bucket, INGEST_API_KEY: "fixture-key", PIPELINE_RECOVERY_ENABLED: "1" };
  const run = (method = "POST", overrides = {}) => checkpointObjectRequest(request(method), { ...runtime, ...overrides }, now);
  return { db, plan, planBytes, scope, planKey, bytes, bucket, objects, calls, sqls, query, run, request, runtime };
}

test("recovery checkpoint binds immutable plan namespace, real indexed SQL and exact readback", async () => {
  const f = fixture();
  try {
    const reply = await f.run(); assert.equal(reply.status, 200);
    const receipt = await reply.json();
    assert.match(receipt.key, new RegExp(`/recovery/fixture-recovery/${f.scope}/`));
    assert.equal(receipt.usage.r2ClassB, 3); assert.equal(receipt.usage.d1RowsRead, null);
    assert.equal(receipt.usage.d1RowsWritten, 0);
    assert.equal((await f.run()).status, 200); assert.equal(f.objects.size, 1);
    const get = await f.run("GET"); assert.equal(get.status, 200);
    assert.deepEqual(new Uint8Array(await get.arrayBuffer()), f.bytes);
    const plan = f.db.prepare(`EXPLAIN QUERY PLAN ${f.sqls[0]}`).all(f.plan.runKey, "owner", 1, "2026-10-02",
      f.plan.recoveryId, "2026-10-03T02:00:30Z", f.plan.planHash, f.scope, f.planKey, "2026-10-03T02:00:30Z");
    assert(plan.filter(row => row.detail.includes("SEARCH")).length >= 2);
    assert(!plan.some(row => row.detail.includes("SCAN")));
  } finally { f.db.close(); }
});

test("flag, authentication and partial recovery identity reject before storage", async () => {
  const f = fixture();
  try {
    assert.equal((await f.run("POST", { PIPELINE_RECOVERY_ENABLED: "0" })).status, 404);
    assert.equal((await checkpointObjectRequest(f.request("POST", "wrong"), f.runtime, now)).status, 401);
    f.query.delete("scopeSha256"); assert.equal((await f.run()).status, 422);
    assert.deepEqual(f.calls, []); assert.deepEqual(f.sqls, []);
  } finally { f.db.close(); }
});

test("revoked approvals, takeover and exact identity mismatches never write", async () => {
  const mutations = [
    "UPDATE pipeline_window_recovery SET status='REVOKED'", "UPDATE pipeline_window_recovery SET expires_at='2026-10-03T02:00:00Z'",
    "UPDATE pipeline_window_recovery SET plan_sha256='wrong'", "UPDATE pipeline_window_recovery SET scope_sha256='wrong'",
    "UPDATE pipeline_window_recovery SET plan_key='wrong'", "UPDATE pipeline_window SET lease_owner='other'",
    "UPDATE pipeline_window SET attempt_count=2", "UPDATE pipeline_window SET target_date='2026-10-01'",
    "UPDATE pipeline_window SET status='COMPLETED'", "UPDATE pipeline_window SET lease_expires_at='2026-10-03T02:00:00Z'",
  ];
  for (const mutation of mutations) {
    const f = fixture(); try { f.db.exec(mutation); assert.equal((await f.run()).status, 409, mutation); assert(!f.calls.includes("put")); }
    finally { f.db.close(); }
  }
});

test("unsupported mappings and expired immutable deadline stay closed", async () => {
  for (const stage of ["build", "dashboard", "handoff", "normalize", "verify_visible"]) {
    const f = fixture(); try { f.query.set("stage", stage); assert.equal((await f.run()).status, 422); assert(!f.calls.includes("put")); }
    finally { f.db.close(); }
  }
  const f = fixture({ deadline: "2026-10-03T02:00:00Z" });
  try { assert.equal((await f.run()).status, 409); assert(!f.calls.includes("put")); } finally { f.db.close(); }
  const g = fixture({ stages: ["publish"] });
  try { assert.equal((await g.run()).status, 409); assert(!g.calls.includes("put")); } finally { g.db.close(); }
});

test("corrupt plan and approval revocation after plan read cannot authorize PUT", async () => {
  const f = fixture();
  try { f.planBytes[0] ^= 1; assert.notEqual((await f.run()).status, 200); assert(!f.calls.includes("put")); } finally { f.db.close(); }
  const g = fixture(); const get = g.bucket.get;
  g.bucket.get = async key => { const result = await get(key); if (key === g.planKey) g.db.exec("UPDATE pipeline_window_recovery SET status='REVOKED'"); return result; };
  try { assert.equal((await g.run()).status, 409); assert(!g.calls.includes("put")); } finally { g.db.close(); }
});

test("orphan immutable write after takeover or revocation is never acknowledged", async () => {
  for (const mutation of ["UPDATE pipeline_window SET lease_owner='other'", "UPDATE pipeline_window_recovery SET status='REVOKED'"]) {
    const f = fixture(); const put = f.bucket.put;
    f.bucket.put = async (...args) => { const result = await put(...args); f.db.exec(mutation); return result; };
    try { assert.equal((await f.run()).status, 409); assert.equal(f.objects.size, 1); assert.equal((await f.run("GET")).status, 409); }
    finally { f.db.close(); }
  }
});
