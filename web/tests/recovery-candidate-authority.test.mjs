import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { canonicalJson } from "../app/lib/pipeline-recovery-plan.ts";
import { candidateUploadAuthority, parseCandidateExecution } from "../app/lib/candidate-upload-authority.ts";

function fixture(stages = ["publish"]) {
  const now = () => new Date("2026-10-03T02:00:00Z");
  const hash = value => createHash("sha256").update(value).digest("hex");
  const plan = { schemaVersion: 1, contractVersion: "recovery-execution-v1", campaignId: "fixture",
    recoveryId: "fixture-recovery", runKey: "daily-market-data:2026-10-03:reconcile:v1",
    dataDates: ["2026-10-02"], requiredStages: stages, artifactRoot: ".recovery/fixture",
    stateDir: ".recovery/fixture/state", maxDates: 1, maxRequests: 20,
    deadline: "2026-10-03T03:00:00Z", budgetManifest: { fixture: true } };
  plan.planHash = hash(canonicalJson(plan));
  const bytes = Buffer.from(canonicalJson(plan) + "\n"), scope = hash(bytes);
  const key = `market-data/recovery/plans/${plan.recoveryId}-${scope}.json`;
  const ref = { runKey: plan.runKey, leaseOwner: "owner", attemptCount: 1, targetDate: "2026-10-02",
    producerBuildSha: "a".repeat(40), recoveryId: plan.recoveryId, planSha256: plan.planHash, scopeSha256: scope };
  const db = new DatabaseSync(":memory:");
  db.exec(`CREATE TABLE pipeline_window (run_key TEXT PRIMARY KEY, lease_owner TEXT, attempt_count INTEGER,
    target_date TEXT, recovery_id TEXT, status TEXT, lease_expires_at TEXT);
    CREATE TABLE pipeline_window_recovery (recovery_id TEXT PRIMARY KEY, run_key TEXT, plan_key TEXT,
    plan_sha256 TEXT, scope_sha256 TEXT, status TEXT, expires_at TEXT);
    CREATE TABLE release_manifest (release_id TEXT PRIMARY KEY, as_of TEXT, schema_version INTEGER,
    manifest_sha256 TEXT, manifest_json TEXT, observed_at TEXT, status TEXT, created_at TEXT);`);
  db.prepare("INSERT INTO pipeline_window VALUES (?,'owner',1,'2026-10-02',?,'RUNNING','2026-10-03T03:00:00Z')").run(plan.runKey, plan.recoveryId);
  db.prepare("INSERT INTO pipeline_window_recovery VALUES (?,?,?,?,?,'ACTIVE','2026-10-03T03:00:00Z')").run(plan.recoveryId, plan.runKey, key, plan.planHash, scope);
  let beforeInsert = () => {}, afterPlan = () => {};
  const sqls = [];
  const adapter = { withSession() { return this; }, prepare(sql) { sqls.push(sql);
    assert.doesNotMatch(sql, /UPDATE|DELETE|DROP|active_release/i);
    return { bind(...args) { return { async first() { return db.prepare(sql).get(...args) ?? null; },
      async run() { beforeInsert(); return db.prepare(sql).run(...args); } }; } }; } };
  const runtime = { enabled: "1", bucket: {
    async head(k) { assert.equal(k, key); return { size: bytes.length, customMetadata: { sha256: scope } }; },
    async get(k) { assert.equal(k, key); afterPlan(); return { arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length) }; },
  } };
  return { db, ref, sqls, runtime, now, authority: () => candidateUploadAuthority(adapter, ref, now, runtime),
    insertHook(fn) { beforeInsert = fn; }, planHook(fn) { afterPlan = fn; } };
}
test("approved recovery stages exact candidate without activation", async () => {
  const f = fixture(); try {
    assert.deepEqual(parseCandidateExecution(f.ref, { schemaVersion: 4, asOf: f.ref.targetDate, producerBuildSha: f.ref.producerBuildSha }, "stage_only"), f.ref);
    const authority = f.authority();
    const release = { releaseId: "fixture-release", asOf: f.ref.targetDate, manifestSha256: "b".repeat(64) };
    assert.equal(await authority.stage(release, { schemaVersion: 4 }, "fixture-json"), "staged_candidate");
    assert.equal(f.db.prepare("SELECT status FROM release_manifest").get().status, "STAGED");
  } finally { f.db.close(); }
});
test("partial identity and disabled recovery cannot gain authority", () => {
  const f = fixture(); try {
    const incomplete = { ...f.ref }; delete incomplete.scopeSha256;
    assert.throws(() => parseCandidateExecution(incomplete, {}, "stage_only"), /invalid_candidate/);
    f.runtime.enabled = "0"; assert.throws(f.authority, /pipeline_recovery_disabled/);
    assert.equal(f.sqls.length, 0);
  } finally { f.db.close(); }
});
test("revocation at conditional INSERT fences candidate registration", async () => {
  const f = fixture(); try {
    f.insertHook(() => f.db.exec("UPDATE pipeline_window_recovery SET status='REVOKED'"));
    await assert.rejects(f.authority().stage({ releaseId: "r", asOf: f.ref.targetDate, manifestSha256: "b".repeat(64) }, { schemaVersion: 4 }, "bytes"), /lease_conflict/);
    assert.equal(f.db.prepare("SELECT count(*) AS n FROM release_manifest").get().n, 0);
  } finally { f.db.close(); }
});
test("unapproved stage and revocation during plan read reject effects", async () => {
  for (const stages of [["core_collection"], ["publish"]]) {
    const f = fixture(stages); let effects = 0;
    try {
      if (stages[0] === "publish") f.planHook(() => f.db.exec("UPDATE pipeline_window SET lease_owner='other'"));
      await assert.rejects(f.authority().effect(async () => effects++));
      assert.equal(effects, 0);
    } finally { f.db.close(); }
  }
});
test("wrong identity, expired approval and takeover reject before effect", async () => {
  for (const mutation of ["UPDATE pipeline_window SET attempt_count=2",
    "UPDATE pipeline_window SET target_date='2026-10-01'",
    "UPDATE pipeline_window SET status='COMPLETED'",
    "UPDATE pipeline_window_recovery SET plan_sha256='wrong'",
    "UPDATE pipeline_window_recovery SET scope_sha256='wrong'",
    "UPDATE pipeline_window_recovery SET expires_at='2026-10-03T02:00:00Z'"]) {
    const f = fixture(); let effects = 0;
    try { f.db.exec(mutation); await assert.rejects(f.authority().effect(async () => effects++)); assert.equal(effects, 0); }
    finally { f.db.close(); }
  }
  const f = fixture(); try {
    await assert.rejects(f.authority().effect(async () => f.db.exec("UPDATE pipeline_window SET lease_owner='other'")), /lease_conflict/);
  } finally { f.db.close(); }
});
