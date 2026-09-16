import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  buildPipelineRunKey,
  claimPipelineWindow,
  getPipelineWindow,
  markPipelineWindow,
} from "../app/lib/pipeline-window.ts";
import {
  claimPipelineWindowRecovery,
  getPipelineWindowRecovery,
  markPipelineWindowRecovery,
  registerPipelineWindowRecovery,
} from "../app/lib/pipeline-window-recovery.ts";
import {
  canonicalJson,
  recoveryPlanObjectKey,
  sha256Hex,
} from "../app/lib/pipeline-recovery-plan.ts";

class D1Statement {
  constructor(database, sql, rowsWritten = 0) {
    this.database = database;
    this.sql = sql;
    this.values = [];
    this.rowsWritten = rowsWritten;
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  run() {
    const result = this.database.prepare(this.sql).run(...this.values);
    const changes = Number(result.changes ?? 0);
    return {
      meta: {
        changes,
        rows_written: changes === 0 ? 0 : Math.max(changes, this.rowsWritten || changes),
      },
    };
  }

  first() {
    return this.database.prepare(this.sql).get(...this.values) ?? null;
  }
}

class LocalD1 {
  constructor({ failAtBatchIndex = null } = {}) {
    this.database = new DatabaseSync(":memory:");
    this.failAtBatchIndex = failAtBatchIndex;
  }

  prepare(sql) {
    return new D1Statement(this.database, sql);
  }

  batch(statements) {
    this.database.exec("BEGIN");
    const results = [];
    try {
      for (let index = 0; index < statements.length; index += 1) {
        if (this.failAtBatchIndex === index) {
          throw new Error("injected_mid_transaction_failure");
        }
        results.push(statements[index].run());
      }
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.database.close();
  }
}

class MemoryR2 {
  constructor() {
    this.objects = new Map();
    this.putCount = 0;
  }

  async head(key) {
    const item = this.objects.get(key);
    if (!item) return null;
    return {
      key,
      size: item.bytes.byteLength,
      customMetadata: { ...item.customMetadata },
      httpMetadata: { ...item.httpMetadata },
    };
  }

  async get(key) {
    const item = this.objects.get(key);
    if (!item) return null;
    const bytes = new Uint8Array(item.bytes);
    return {
      key,
      size: bytes.byteLength,
      customMetadata: { ...item.customMetadata },
      async arrayBuffer() {
        return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      },
    };
  }

  async put(key, value, options = {}) {
    let bytes;
    if (typeof value === "string") {
      bytes = new TextEncoder().encode(value);
    } else if (value instanceof Uint8Array) {
      bytes = new Uint8Array(value);
    } else if (value instanceof ArrayBuffer) {
      bytes = new Uint8Array(value.slice(0));
    } else if (value && typeof value.arrayBuffer === "function") {
      bytes = new Uint8Array(await value.arrayBuffer());
    } else {
      throw new Error("unsupported_r2_value");
    }
    this.putCount += 1;
    this.objects.set(key, {
      bytes,
      customMetadata: { ...(options.customMetadata ?? {}) },
      httpMetadata: { ...(options.httpMetadata ?? {}) },
    });
    return this.head(key);
  }

  seed(key, bytes, customMetadata) {
    this.objects.set(key, {
      bytes: new Uint8Array(bytes),
      customMetadata: { ...customMetadata },
      httpMetadata: { contentType: "application/json" },
    });
  }
}

async function applyMigration(db, name) {
  const text = await readFile(new URL("../drizzle/" + name, import.meta.url), "utf8");
  for (const statement of text.split("--> statement-breakpoint")) {
    const sql = statement.trim();
    if (sql) db.database.exec(sql);
  }
}

async function createDatabase(options = {}) {
  const db = new LocalD1(options);
  await applyMigration(db, "0010_fair_jasper_sitwell.sql");
  await applyMigration(db, "0011_futuristic_namora.sql");
  await applyMigration(db, "0012_boring_tyger_tiger.sql");
  return db;
}

function input(runKey, owner = "recovery:owner01", extras = {}) {
  return {
    recoveryId: extras.recoveryId ?? "recovery-20260903-01",
    runKey,
    leaseOwner: owner,
    ...extras,
  };
}

function registrationInput(runKey, row, recoveryId = "recovery-20260903-01", extras = {}) {
  return {
    recoveryId,
    runKey,
    planKey: "repair-plan-20260903",
    planSha256: "a".repeat(64),
    scopeSha256: "b".repeat(64),
    expectedAttemptCount: Number(row.attempt_count),
    expectedUpdatedAt: row.updated_at,
    previousStateJson: JSON.stringify({
      status: row.status,
      attemptCount: row.attempt_count,
      updatedAt: row.updated_at,
      recoveryId: row.recovery_id ?? null,
    }),
    reasonCode: "controlled_recovery",
    approvalReference: "approval-20260903",
    authorizedAt: "2026-09-03T00:00:00.000Z",
    expiresAt: "2026-09-04T23:59:59.000Z",
    ...extras,
  };
}

async function recoveryPlanFixture(runKey, recoveryId = "recovery-20260903-01") {
  const base = {
    artifactRoot: `.recovery/${recoveryId}`,
    budgetManifest: { schemaVersion: 1, source: "test-fixture" },
    campaignId: "flow-recovery-test",
    contractVersion: "recovery-execution-v1",
    dataDates: ["2026-09-03"],
    deadline: "2026-09-30T00:00:00Z",
    maxDates: 1,
    maxRequests: 50,
    recoveryId,
    requiredStages: ["core_collection", "dashboard", "publish"],
    runKey,
    schemaVersion: 1,
    stateDir: `.recovery/${recoveryId}/state`,
  };
  const planSha256 = await sha256Hex(new TextEncoder().encode(canonicalJson(base)));
  const plan = { ...base, planHash: planSha256 };
  const planJson = JSON.stringify(plan);
  const scopeSha256 = await sha256Hex(new TextEncoder().encode(planJson));
  return {
    plan,
    planJson,
    planSha256,
    scopeSha256,
    planKey: recoveryPlanObjectKey(recoveryId, scopeSha256),
  };
}

async function completedWindow(db, windowName = "evening") {
  const now = new Date("2026-09-03T01:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-09-03", windowName);
  await claimPipelineWindow(db, {
    runKey,
    windowDate: "2026-09-03",
    windowName,
    contractVersion: "v1",
    targetDate: "2026-09-03",
    leaseOwner: "normal:owner01",
  }, now);
  await markPipelineWindow(db, {
    runKey,
    leaseOwner: "normal:owner01",
    status: "COMPLETED",
    completionAsOf: "2026-09-03",
    publicationStatus: "confirmed",
  }, new Date("2026-09-03T01:00:01.000Z"));
  const row = await getPipelineWindow(db, runKey);
  assert.ok(row);
  assert.equal(row.status, "COMPLETED");
  assert.equal(row.recovery_id, null);
  return { now, runKey, row };
}

async function registerCompleted(db, windowName = "evening", recoveryId = "recovery-20260903-01", extras = {}) {
  const setup = await completedWindow(db, windowName);
  const registration = await registerPipelineWindowRecovery(
    db,
    registrationInput(setup.runKey, setup.row, recoveryId, extras),
    new Date("2026-09-03T02:00:00.000Z"),
  );
  return { ...setup, registration };
}

test("registration is immutable, idempotent and does not bind the pipeline row", async () => {
  const db = await createDatabase();
  try {
    const first = await registerCompleted(db);
    assert.equal(first.registration.decision, "registered");
    assert.equal(first.registration.recovery.status, "AUTHORIZED");
    const unbound = await getPipelineWindow(db, first.runKey);
    assert.equal(unbound.recovery_id, null);
    assert.equal(unbound.status, first.row.status);
    assert.equal(unbound.attempt_count, first.row.attempt_count);
    assert.equal(unbound.updated_at, first.row.updated_at);

    const second = await registerPipelineWindowRecovery(
      db,
      registrationInput(first.runKey, first.row),
      new Date("2026-09-03T02:01:00.000Z"),
    );
    assert.equal(second.decision, "already_registered");
    assert.equal(second.recovery.recovery_id, first.registration.recovery.recovery_id);

    await assert.rejects(
      registerPipelineWindowRecovery(
        db,
        registrationInput(first.runKey, first.row, first.registration.recovery.recovery_id, {
          scopeSha256: "c".repeat(64),
        }),
        new Date("2026-09-03T02:01:00.000Z"),
      ),
      /recovery_registration_conflict/,
    );
    await assert.rejects(
      registerPipelineWindowRecovery(
        db,
        registrationInput(first.runKey, first.row, first.registration.recovery.recovery_id, {
          previousStateJson: JSON.stringify({ status: "RUNNING" }),
        }),
        new Date("2026-09-03T02:01:00.000Z"),
      ),
      /recovery_registration_conflict/,
    );

    await assert.rejects(
      registerPipelineWindowRecovery(
        db,
        registrationInput(first.runKey, first.row, "recovery-20260903-02"),
        new Date("2026-09-03T02:01:00.000Z"),
      ),
      /recovery_scope_conflict/,
    );
    const recovery = await getPipelineWindowRecovery(db, first.registration.recovery.recovery_id);
    assert.equal(recovery.status, "AUTHORIZED");
  } finally {
    db.close();
  }
});

test("twenty concurrent recovery claims produce exactly one acquired result", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    const claimNow = new Date("2026-09-03T03:00:00.000Z");
    const results = await Promise.all(
      Array.from({ length: 20 }, (_, index) => claimPipelineWindowRecovery(
        db,
        input(setup.runKey, "recovery:owner" + String(index + 1).padStart(2, "0")),
        claimNow,
      )),
    );
    assert.equal(results.filter((result) => result.decision === "acquired").length, 1);
    assert.equal(results.filter((result) => result.decision === "skip_leased").length, 19);
    assert.equal(results.filter((result) => result.decision === "takeover").length, 0);
    assert.equal(results.find((result) => result.decision === "acquired").rowsWritten, 2);
    const row = await getPipelineWindow(db, setup.runKey);
    const recovery = await getPipelineWindowRecovery(db, setup.registration.recovery.recovery_id);
    assert.equal(row.recovery_id, setup.registration.recovery.recovery_id);
    assert.equal(row.attempt_count, 2);
    assert.equal(recovery.status, "ACTIVE");
  } finally {
    db.close();
  }
});

test("completed recovery claims are idempotent and a valid lease cannot be stolen", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    const first = await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner01"), new Date("2026-09-03T03:00:00.000Z"));
    const leased = await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner02"), new Date("2026-09-03T03:01:00.000Z"));
    assert.equal(first.decision, "acquired");
    assert.equal(leased.decision, "skip_leased");

    const completed = await markPipelineWindowRecovery(db, {
      recoveryId: setup.registration.recovery.recovery_id,
      runKey: setup.runKey,
      leaseOwner: "recovery:owner01",
      expectedAttemptCount: 2,
      status: "COMPLETED",
      latestReceiptKey: "receipts/2026-09-03.json",
    }, new Date("2026-09-03T03:05:00.000Z"));
    assert.equal(completed.recovery.status, "COMPLETED");
    const repeat = await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner03"), new Date("2026-09-03T03:06:00.000Z"));
    assert.equal(repeat.decision, "already_satisfied");
    assert.equal(repeat.rowsWritten, 0);
  } finally {
    db.close();
  }
});

test("running recovery can renew its lease without changing the attempt", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner01"),
      new Date("2026-09-03T03:00:00.000Z"),
    );
    const renewed = await markPipelineWindowRecovery(
      db,
      {
        recoveryId: setup.registration.recovery.recovery_id,
        runKey: setup.runKey,
        leaseOwner: "recovery:owner01",
        expectedAttemptCount: 2,
        status: "RUNNING",
        renewLease: true,
      },
      new Date("2026-09-03T03:05:00.000Z"),
    );
    assert.equal(renewed.row.status, "RUNNING");
    assert.equal(renewed.row.attempt_count, 2);
    assert.equal(renewed.row.lease_expires_at, "2026-09-03T03:20:00.000Z");
    const lateOwner = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner02"),
      new Date("2026-09-03T03:16:00.000Z"),
    );
    assert.equal(lateOwner.decision, "skip_leased");
  } finally {
    db.close();
  }
});

test("renewLease is rejected for terminal recovery status", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner01"),
      new Date("2026-09-03T03:00:00.000Z"),
    );
    await assert.rejects(
      markPipelineWindowRecovery(
        db,
        {
          recoveryId: setup.registration.recovery.recovery_id,
          runKey: setup.runKey,
          leaseOwner: "recovery:owner01",
          expectedAttemptCount: 2,
          status: "FAILED_RETRYABLE",
          renewLease: true,
        },
        new Date("2026-09-03T03:05:00.000Z"),
      ),
      /invalid_renew_lease/,
    );
  } finally {
    db.close();
  }
});

test("expired recovery lease can be taken over and late callbacks are rejected", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner01"), new Date("2026-09-03T03:00:00.000Z"));
    const takeover = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner02"),
      new Date("2026-09-03T03:16:00.001Z"),
    );
    assert.equal(takeover.decision, "takeover");
    assert.equal(takeover.attemptCount, 3);
    assert.equal(takeover.leaseOwner, "recovery:owner02");

    await assert.rejects(
      markPipelineWindowRecovery(db, {
        recoveryId: setup.registration.recovery.recovery_id,
        runKey: setup.runKey,
        leaseOwner: "recovery:owner01",
        expectedAttemptCount: 2,
        status: "FAILED_RETRYABLE",
        errorCode: "late_callback",
      }, new Date("2026-09-03T03:17:00.000Z")),
      /recovery_status_conflict/,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    assert.equal(row.lease_owner, "recovery:owner02");
    assert.equal(row.attempt_count, 3);
  } finally {
    db.close();
  }
});

test("callbacks after lease expiry are rejected before any takeover", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner01"), new Date("2026-09-03T03:00:00.000Z"));
    await assert.rejects(
      markPipelineWindowRecovery(db, {
        recoveryId: setup.registration.recovery.recovery_id,
        runKey: setup.runKey,
        leaseOwner: "recovery:owner01",
        expectedAttemptCount: 2,
        status: "COMPLETED",
        latestReceiptKey: "receipts/late.json",
      }, new Date("2026-09-03T03:15:00.001Z")),
      /recovery_lease_expired/,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    const recovery = await getPipelineWindowRecovery(db, setup.registration.recovery.recovery_id);
    assert.equal(row.status, "DISPATCHED");
    assert.equal(row.recovery_id, setup.registration.recovery.recovery_id);
    assert.equal(recovery.status, "ACTIVE");
  } finally {
    db.close();
  }
});

test("retryable recovery can be resumed, while blocked quality remains blocked", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner01"), new Date("2026-09-03T03:00:00.000Z"));
    await markPipelineWindowRecovery(db, {
      recoveryId: setup.registration.recovery.recovery_id,
      runKey: setup.runKey,
      leaseOwner: "recovery:owner01",
      expectedAttemptCount: 2,
      status: "FAILED_RETRYABLE",
      errorCode: "source_not_ready",
    }, new Date("2026-09-03T03:05:00.000Z"));
    const retry = await claimPipelineWindowRecovery(db, input(setup.runKey, "recovery:owner02"), new Date("2026-09-03T03:06:00.000Z"));
    assert.equal(retry.decision, "takeover");
    assert.equal(retry.attemptCount, 3);

    await markPipelineWindowRecovery(db, {
      recoveryId: setup.registration.recovery.recovery_id,
      runKey: setup.runKey,
      leaseOwner: "recovery:owner02",
      expectedAttemptCount: 3,
      status: "BLOCKED_QUALITY",
      errorCode: "quality_gate_blocked",
      sourceFingerprint: "fingerprint-old",
    }, new Date("2026-09-03T03:07:00.000Z"));
    const blocked = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner03", { sourceFingerprint: "fingerprint-new" }),
      new Date("2026-09-03T03:08:00.000Z"),
    );
    assert.equal(blocked.decision, "skip_blocked");
    assert.equal(blocked.recoveryStatus, "BLOCKED");
  } finally {
    db.close();
  }
});

test("a retryable row with a live lease cannot be reclaimed early", async () => {
  const db = await createDatabase();
  try {
    const setup = await completedWindow(db);
    db.database.prepare(
      "UPDATE pipeline_window SET status = 'FAILED_RETRYABLE', lease_owner = ?, lease_expires_at = ?, updated_at = ? WHERE run_key = ?",
    ).run(
      "normal:owner01",
      "2026-09-03T03:15:00.000Z",
      "2026-09-03T03:00:01.000Z",
      setup.runKey,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    const registration = await registerPipelineWindowRecovery(
      db,
      registrationInput(setup.runKey, row, "recovery-20260903-live-retry", {
        planSha256: "e".repeat(64),
        scopeSha256: "f".repeat(64),
      }),
      new Date("2026-09-03T03:01:00.000Z"),
    );
    await assert.rejects(
      claimPipelineWindowRecovery(
        db,
        input(setup.runKey, "recovery:owner01", { recoveryId: registration.recovery.recovery_id }),
        new Date("2026-09-03T03:05:00.000Z"),
      ),
      /recovery_state_not_eligible/,
    );
    const takeover = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner02", { recoveryId: registration.recovery.recovery_id }),
      new Date("2026-09-03T03:16:00.001Z"),
    );
    assert.equal(takeover.decision, "acquired");
  } finally {
    db.close();
  }
});

test("ordinary claim and status cannot consume a recovery-bound window", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    const recoveryClaim = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner01"),
      new Date("2026-09-03T03:00:00.000Z"),
    );
    assert.equal(recoveryClaim.decision, "acquired");
    const ordinaryClaim = await claimPipelineWindow(db, {
      runKey: setup.runKey,
      windowDate: "2026-09-03",
      windowName: "evening",
      contractVersion: "v1",
      targetDate: "2026-09-03",
      leaseOwner: "normal:owner02",
    }, new Date("2026-09-03T03:01:00.000Z"));
    assert.equal(ordinaryClaim.decision, "skip_leased");
    await assert.rejects(
      markPipelineWindow(db, {
        runKey: setup.runKey,
        leaseOwner: "recovery:owner01",
        status: "RUNNING",
      }, new Date("2026-09-03T03:02:00.000Z")),
      /pipeline_window_status_conflict/,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    assert.equal(row.recovery_id, setup.registration.recovery.recovery_id);
    assert.equal(row.status, "DISPATCHED");
  } finally {
    db.close();
  }
});

test("stale CAS rolls back both sides and leaves recovery unbound", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    db.database.prepare("UPDATE pipeline_window SET updated_at = ? WHERE run_key = ?").run(
      "2026-09-03T03:00:01.000Z",
      setup.runKey,
    );
    await assert.rejects(
      claimPipelineWindowRecovery(db, input(setup.runKey), new Date("2026-09-03T03:00:00.000Z")),
      /recovery_claim_conflict/,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    const recovery = await getPipelineWindowRecovery(db, setup.registration.recovery.recovery_id);
    assert.equal(row.recovery_id, null);
    assert.equal(row.attempt_count, setup.row.attempt_count);
    assert.equal(recovery.status, "AUTHORIZED");
  } finally {
    db.close();
  }
});

test("mid-transaction failure rolls back the pipeline mutation", async () => {
  const db = await createDatabase({ failAtBatchIndex: 2 });
  try {
    const setup = await registerCompleted(db);
    await assert.rejects(
      claimPipelineWindowRecovery(db, input(setup.runKey), new Date("2026-09-03T03:00:00.000Z")),
      /injected_mid_transaction_failure/,
    );
    const row = await getPipelineWindow(db, setup.runKey);
    const recovery = await getPipelineWindowRecovery(db, setup.registration.recovery.recovery_id);
    assert.equal(row.recovery_id, null);
    assert.equal(row.status, "COMPLETED");
    assert.equal(recovery.status, "AUTHORIZED");
  } finally {
    db.close();
  }
});

test("invalid run keys and expired approvals are rejected", async () => {
  const db = await createDatabase();
  try {
    const setup = await completedWindow(db);
    await assert.rejects(
      registerPipelineWindowRecovery(db, registrationInput(
        "daily-market-data:2026-09-03:unknown:v1",
        setup.row,
        "recovery-invalid-window",
      )),
      /invalid_run_key/,
    );
    const registration = await registerPipelineWindowRecovery(
      db,
      registrationInput(setup.runKey, setup.row, "recovery-expired", {
        planSha256: "c".repeat(64),
        expiresAt: "2026-09-03T02:30:00.000Z",
      }),
      new Date("2026-09-03T02:00:00.000Z"),
    );
    await assert.rejects(
      claimPipelineWindowRecovery(
        db,
        input(setup.runKey, "recovery:owner01", { recoveryId: registration.recovery.recovery_id }),
        new Date("2026-09-03T03:00:00.000Z"),
      ),
      /recovery_approval_expired/,
    );
  } finally {
    db.close();
  }
});

test("a new plan revision can be registered while an older authorization is still unclaimed", async () => {
  const db = await createDatabase();
  try {
    const setup = await registerCompleted(db);
    const revision = await registerPipelineWindowRecovery(
      db,
      registrationInput(setup.runKey, setup.row, "recovery-20260903-02", {
        planSha256: "c".repeat(64),
        scopeSha256: "d".repeat(64),
      }),
      new Date("2026-09-03T02:01:00.000Z"),
    );
    assert.equal(revision.decision, "registered");
    assert.equal(revision.recovery.status, "AUTHORIZED");

    const firstClaim = await claimPipelineWindowRecovery(
      db,
      input(setup.runKey, "recovery:owner01"),
      new Date("2026-09-03T03:00:00.000Z"),
    );
    assert.equal(firstClaim.decision, "acquired");
    await assert.rejects(
      claimPipelineWindowRecovery(
        db,
        input(setup.runKey, "recovery:owner02", { recoveryId: revision.recovery.recovery_id }),
        new Date("2026-09-03T03:01:00.000Z"),
      ),
      /pipeline_window_recovery_bound/,
    );
  } finally {
    db.close();
  }
});

test("recovery routes fail closed when disabled and require existing authentication", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("pipeline-window-recovery-test", String(process.pid) + "-" + String(Date.now()));
  const loadedWorker = await import(workerUrl.href);
  const worker = loadedWorker.default;
  const makeRequest = () => new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
    method: "POST",
    body: "{}",
    headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
  });
  const context = { waitUntil() {}, passThroughOnException() {} };
  const disabled = await worker.fetch(makeRequest(), {
    INGEST_API_KEY: "test-key",
    PIPELINE_RECOVERY_ENABLED: "0",
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, context);
  assert.equal(disabled.status, 404);

  const unauthenticated = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
    method: "POST",
    body: "{}",
    headers: { "Content-Type": "application/json" },
  }), {
    INGEST_API_KEY: "test-key",
    PIPELINE_RECOVERY_ENABLED: "1",
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, context);
  assert.equal(unauthenticated.status, 401);

  const db = await createDatabase();
  try {
    const setup = await completedWindow(db);
    const bucket = new MemoryR2();
    const plan = await recoveryPlanFixture(setup.runKey);
    const recoveryBody = registrationInput(setup.runKey, setup.row, "recovery-20260903-01", {
      planKey: undefined,
      planSha256: plan.planSha256,
      scopeSha256: plan.scopeSha256,
      planJson: plan.planJson,
      authorizedAt: new Date(Date.now() - 1_000).toISOString(),
      expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    });
    const registerResponse = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
      method: "POST",
      body: JSON.stringify(recoveryBody),
      headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
    }), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(registerResponse.status, 201);
    const registered = await registerResponse.json();
    assert.equal(registered.planKey, plan.planKey);
    assert.equal(registered.planStorageDecision, "stored");
    assert.equal(bucket.putCount, 1);

    const repeatResponse = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
      method: "POST",
      body: JSON.stringify(recoveryBody),
      headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
    }), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(repeatResponse.status, 200);
    const repeated = await repeatResponse.json();
    assert.equal(repeated.decision, "already_registered");
    assert.equal(repeated.planStorageDecision, "already_stored");
    assert.equal(bucket.putCount, 1);

    const planResponse = await worker.fetch(new Request(
      `http://localhost/api/v1/admin/pipeline-window/recovery/plan?recoveryId=${recoveryBody.recoveryId}`,
      { headers: { Authorization: "Bearer test-key" } },
    ), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(planResponse.status, 200);
    const readBack = await planResponse.json();
    assert.equal(readBack.plan.planHash, plan.planSha256);
    assert.equal(readBack.scopeSha256, plan.scopeSha256);

    const claimResponse = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery/claim", {
      method: "POST",
      body: JSON.stringify({
        recoveryId: recoveryBody.recoveryId,
        runKey: setup.runKey,
        leaseOwner: "recovery:owner01",
      }),
      headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
    }), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(claimResponse.status, 200);
    assert.equal((await claimResponse.json()).decision, "acquired");
  } finally {
    db.close();
  }
});

test("recovery registration rejects invalid bytes and conflicting R2 metadata before D1 authorization", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("pipeline-window-recovery-plan-test", String(process.pid) + "-" + String(Date.now()));
  const worker = (await import(workerUrl.href)).default;
  const context = { waitUntil() {}, passThroughOnException() {} };
  const db = await createDatabase();
  try {
    const setup = await completedWindow(db);
    const plan = await recoveryPlanFixture(setup.runKey, "recovery-invalid-plan");
    const bucket = new MemoryR2();
    const body = registrationInput(setup.runKey, setup.row, "recovery-invalid-plan", {
      planKey: undefined,
      planSha256: plan.planSha256,
      scopeSha256: plan.scopeSha256,
      planJson: plan.planJson + " ",
      authorizedAt: new Date(Date.now() - 1_000).toISOString(),
      expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    });
    const invalid = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
    }), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(invalid.status, 422);
    assert.equal((await invalid.json()).error, "plan_object_sha256_mismatch");
    assert.equal(bucket.putCount, 0);
    assert.equal(await getPipelineWindowRecovery(db, body.recoveryId), null);

    bucket.seed(plan.planKey, new TextEncoder().encode(plan.planJson), {
      sha256: plan.scopeSha256,
      recoveryId: body.recoveryId,
      runKey: setup.runKey,
      planSha256: "f".repeat(64),
    });
    body.planJson = plan.planJson;
    const conflict = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/recovery", {
      method: "POST",
      body: JSON.stringify(body),
      headers: { Authorization: "Bearer test-key", "Content-Type": "application/json" },
    }), {
      DB: db,
      RAW_BUCKET: bucket,
      INGEST_API_KEY: "test-key",
      PIPELINE_RECOVERY_ENABLED: "1",
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, context);
    assert.equal(conflict.status, 409);
    assert.equal((await conflict.json()).error, "plan_object_not_verified");
    assert.equal(await getPipelineWindowRecovery(db, body.recoveryId), null);
  } finally {
    db.close();
  }
});

test("manual recovery-plan workflow can only register and verify one immutable plan", async () => {
  const workflow = await readFile(new URL("../../.github/workflows/register-recovery-plan.yml", import.meta.url), "utf8");
  assert.match(workflow, /workflow_dispatch:/);
  assert.match(workflow, /Register plan through the audited control plane/);
  assert.match(workflow, /recovery\/plan\?recoveryId=/);
  // jq -n has no input object: every envelope property must explicitly
  // reference its --arg/--rawfile variable. Naked object shorthand silently
  // serializes nulls and makes the control-plane request fail closed.
  assert.match(workflow, /\{recoveryId:\$recoveryId, runKey:\$runKey, planSha256:\$planSha256, scopeSha256:\$scopeSha256/);
  assert.doesNotMatch(workflow, /\{recoveryId, runKey, planSha256, scopeSha256, planJson,/);
  assert.doesNotMatch(workflow, /^\s+schedule:/m);
  assert.doesNotMatch(workflow, /repository_dispatch|workflow_run|actions:\s*write|gh workflow run|market-data-(?:collect|publish|watchdog)\.yml/);
});
