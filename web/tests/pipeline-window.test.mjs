import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  bindPipelineTargetDate,
  buildPipelineRunKey,
  claimPipelineWindow,
  getPipelineWindow,
  markPipelineWindow,
  parsePipelineRunKey,
  shanghaiDate,
} from "../app/lib/pipeline-window.ts";

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("pipeline-window-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker;
}

class D1Statement {
  constructor(database, sql, claimMeta) {
    this.database = database;
    this.sql = sql;
    this.claimMeta = claimMeta;
    this.values = [];
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  async run() {
    const result = this.database.prepare(this.sql).run(...this.values);
    const changes = Number(result.changes ?? 0);
    const defaultMeta = {
      changes,
      rows_written: changes,
    };
    return {
      meta: this.sql.includes("INSERT INTO pipeline_window") && this.claimMeta
        ? this.claimMeta({ ...defaultMeta })
        : defaultMeta,
    };
  }

  async first() {
    return this.database.prepare(this.sql).get(...this.values) ?? null;
  }
}

class LocalD1 {
  constructor({ claimMeta } = {}) {
    this.database = new DatabaseSync(":memory:");
    this.claimMeta = claimMeta;
  }

  prepare(sql) {
    return new D1Statement(this.database, sql, this.claimMeta);
  }

  close() {
    this.database.close();
  }
}

async function createDatabase(options = {}) {
  const db = new LocalD1(options);
  for (const migrationName of ["0010_fair_jasper_sitwell.sql", "0011_futuristic_namora.sql", "0012_boring_tyger_tiger.sql"]) {
    const migration = await readFile(new URL(`../drizzle/${migrationName}`, import.meta.url), "utf8");
    for (const statement of migration.split("--> statement-breakpoint")) {
      const sql = statement.trim();
      if (sql) db.database.exec(sql);
    }
  }
  return db;
}

function input(runKey, windowName = "evening", owner = "runner:00000001", extras = {}) {
  return {
    runKey,
    windowDate: "2026-08-29",
    windowName,
    contractVersion: "v1",
    targetDate: "2026-08-29",
    leaseOwner: owner,
    ...extras,
  };
}

function indexedClaimMeta(rowsWritten) {
  return ({ changes }) => ({
    changes,
    rows_written: changes === 1 ? rowsWritten : 0,
  });
}

test("runKey is stable, parseable and uses Shanghai window dates", () => {
  const runKey = buildPipelineRunKey("2026-08-29", "evening");
  assert.equal(runKey, "daily-market-data:2026-08-29:evening:v1");
  assert.deepEqual(parsePipelineRunKey(runKey), {
    windowDate: "2026-08-29",
    windowName: "evening",
    contractVersion: "v1",
  });
  assert.equal(shanghaiDate(Date.parse("2026-08-28T16:30:00.000Z")), "2026-08-29");
  assert.equal(parsePipelineRunKey("daily-market-data:2026-08-29:unknown:v1"), null);
  assert.equal(parsePipelineRunKey("daily-market-data:2026-08-29:evening:v2"), null);
});

test("twenty concurrent claims produce exactly one winner", async () => {
  const db = await createDatabase({ claimMeta: indexedClaimMeta(2) });
  const now = new Date("2026-08-29T00:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-08-29", "evening");
  const results = await Promise.all(
    Array.from({ length: 20 }, (_, index) => claimPipelineWindow(
      db,
      input(runKey, "evening", `runner:${String(index + 1).padStart(8, "0")}`),
      now,
    )),
  );
  assert.equal(results.filter((result) => result.decision === "acquired").length, 1);
  assert.equal(results.filter((result) => result.decision === "takeover").length, 0);
  assert.equal(results.filter((result) => result.decision === "skip_leased").length, 19);
  assert.equal(results.find((result) => result.decision === "acquired").rowsWritten, 2);
  assert.ok(results.every((result) => Number.isSafeInteger(result.rowsWritten) && result.rowsWritten >= 0));
  db.close();
});

test("claim accepts physical index writes while preserving one-row decisions", async () => {
  const acquiredDb = await createDatabase({ claimMeta: indexedClaimMeta(2) });
  const now = new Date("2026-08-29T00:00:00.000Z");
  const acquiredKey = buildPipelineRunKey("2026-08-29", "morning");
  const acquired = await claimPipelineWindow(acquiredDb, input(acquiredKey, "morning"), now);
  assert.equal(acquired.decision, "acquired");
  assert.equal(acquired.rowsWritten, 2);
  acquiredDb.close();

  const takeoverDb = await createDatabase({ claimMeta: indexedClaimMeta(3) });
  const takeoverKey = buildPipelineRunKey("2026-08-29", "reconcile");
  await claimPipelineWindow(takeoverDb, input(takeoverKey, "reconcile"), now);
  const takeover = await claimPipelineWindow(
    takeoverDb,
    input(takeoverKey, "reconcile", "runner:00000002"),
    new Date(now.getTime() + 15 * 60 * 1_000 + 1),
  );
  assert.equal(takeover.decision, "takeover");
  assert.equal(takeover.rowsWritten, 3);
  takeoverDb.close();
});

test("claim fails closed on malformed D1 write metadata", async () => {
  const metadataCases = [
    { name: "missing changes", meta: { rows_written: 2 }, error: /pipeline_window_change_meta_invalid/ },
    { name: "negative changes", meta: { changes: -1, rows_written: 2 }, error: /pipeline_window_change_meta_invalid/ },
    { name: "fractional changes", meta: { changes: 0.5, rows_written: 2 }, error: /pipeline_window_change_meta_invalid/ },
    { name: "multiple logical changes", meta: { changes: 2, rows_written: 2 }, error: /pipeline_window_multirow_mutation/ },
    { name: "missing rows written", meta: { changes: 1 }, error: /pipeline_window_write_meta_invalid/ },
    { name: "negative rows written", meta: { changes: 1, rows_written: -1 }, error: /pipeline_window_write_meta_invalid/ },
    { name: "fractional rows written", meta: { changes: 1, rows_written: 1.5 }, error: /pipeline_window_write_meta_invalid/ },
  ];

  for (const { name, meta, error } of metadataCases) {
    const db = await createDatabase({ claimMeta: () => meta });
    const runKey = buildPipelineRunKey("2026-08-29", "evening");
    try {
      await assert.rejects(claimPipelineWindow(db, input(runKey), new Date("2026-08-29T00:00:00.000Z")), error, name);
    } finally {
      db.close();
    }
  }
});

test("completed, retryable and blocked states are idempotent", async () => {
  const db = await createDatabase();
  const now = new Date("2026-08-29T00:00:00.000Z");

  const completedKey = buildPipelineRunKey("2026-08-29", "reconcile");
  await claimPipelineWindow(db, input(completedKey, "reconcile"), now);
  await markPipelineWindow(db, {
    runKey: completedKey,
    leaseOwner: "runner:00000001",
    status: "COMPLETED",
    completionAsOf: "2026-08-29",
    publicationStatus: "confirmed",
  }, now);
  assert.equal((await claimPipelineWindow(db, input(completedKey, "reconcile", "runner:00000002"), now)).decision, "skip_completed");

  const retryKey = buildPipelineRunKey("2026-08-29", "morning");
  await claimPipelineWindow(db, input(retryKey, "morning"), now);
  await markPipelineWindow(db, {
    runKey: retryKey,
    leaseOwner: "runner:00000001",
    status: "FAILED_RETRYABLE",
    errorCode: "source_not_ready",
  }, now);
  assert.equal((await claimPipelineWindow(db, input(retryKey, "morning", "runner:00000002"), now)).decision, "takeover");

  const blockedKey = buildPipelineRunKey("2026-08-29", "morning_exact");
  await claimPipelineWindow(db, input(blockedKey, "morning_exact", "runner:00000001", { sourceFingerprint: "fp-old" }), now);
  await markPipelineWindow(db, {
    runKey: blockedKey,
    leaseOwner: "runner:00000001",
    status: "BLOCKED_QUALITY",
    errorCode: "quality_gate_blocked",
    sourceFingerprint: "fp-old",
  }, now);
  assert.equal((await claimPipelineWindow(db, input(blockedKey, "morning_exact", "runner:00000002", { sourceFingerprint: "fp-old" }), now)).decision, "skip_blocked_quality");
  assert.equal((await claimPipelineWindow(db, input(blockedKey, "morning_exact", "runner:00000004"), now)).decision, "skip_blocked_quality");
  assert.equal((await claimPipelineWindow(db, input(blockedKey, "morning_exact", "runner:00000003", { sourceFingerprint: "fp-new" }), now)).decision, "takeover");

  const row = await getPipelineWindow(db, blockedKey);
  assert.equal(row.status, "DISPATCHED");
  assert.equal(row.attempt_count, 2);
  db.close();
});

test("an expired lease can be taken over atomically", async () => {
  const db = await createDatabase();
  const claimedAt = new Date("2026-08-29T00:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-08-29", "evening");
  await claimPipelineWindow(db, input(runKey), claimedAt);
  const takeover = await claimPipelineWindow(
    db,
    input(runKey, "evening", "runner:00000002"),
    new Date(claimedAt.getTime() + 15 * 60 * 1_000 + 1),
  );
  assert.equal(takeover.decision, "takeover");
  assert.equal(takeover.leaseOwner, "runner:00000002");
  assert.equal(takeover.attemptCount, 2);
  db.close();
});

test("ordinary completion requires an exact confirmed target-date proof", async () => {
  const db = await createDatabase();
  const now = new Date("2026-08-29T00:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-08-29", "evening");
  await claimPipelineWindow(db, input(runKey), now);

  await assert.rejects(
    markPipelineWindow(db, {
      runKey,
      leaseOwner: "runner:00000001",
      status: "COMPLETED",
    }, now),
    /pipeline_window_completion_proof_required/,
  );
  await assert.rejects(
    markPipelineWindow(db, {
      runKey,
      leaseOwner: "runner:00000001",
      status: "COMPLETED",
      completionAsOf: "2026-08-28",
      publicationStatus: "confirmed",
    }, now),
    /pipeline_window_completion_date_mismatch/,
  );
  await assert.rejects(
    markPipelineWindow(db, {
      runKey,
      leaseOwner: "runner:00000001",
      status: "COMPLETED",
      completionAsOf: "2026-08-29",
      publicationStatus: "pending",
    }, now),
    /pipeline_window_completion_not_confirmed/,
  );

  const completed = await markPipelineWindow(db, {
    runKey,
    leaseOwner: "runner:00000001",
    status: "COMPLETED",
    completionAsOf: "2026-08-29",
    publicationStatus: "confirmed",
  }, now);
  assert.equal(completed.status, "COMPLETED");
  db.close();
});

test("target-date binding is one-time, lease guarded and completion compatible", async () => {
  const db = await createDatabase();
  const now = new Date("2026-09-16T00:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-09-16", "reconcile");
  const owner = "runner:00000001";
  await claimPipelineWindow(db, input(runKey, "reconcile", owner, { windowDate: "2026-09-16", targetDate: null }), now);

  await assert.rejects(
    markPipelineWindow(db, {
      runKey,
      leaseOwner: owner,
      status: "COMPLETED",
      completionAsOf: "2026-09-15",
      publicationStatus: "confirmed",
    }, now),
    /pipeline_window_target_date_missing/,
  );

  const bound = await bindPipelineTargetDate(db, {
    runKey,
    leaseOwner: owner,
    targetDate: "2026-09-15",
    sourceFingerprint: "sha256:source-20260915",
  }, now);
  assert.equal(bound.decision, "bound");
  assert.equal(bound.targetDate, "2026-09-15");
  assert.equal(bound.rowsWritten, 1);

  const repeated = await bindPipelineTargetDate(db, {
    runKey,
    leaseOwner: owner,
    targetDate: "2026-09-15",
    sourceFingerprint: "sha256:other-fingerprint",
  }, now);
  assert.equal(repeated.decision, "already_bound");
  assert.equal(repeated.rowsWritten, 0);
  assert.equal((await getPipelineWindow(db, runKey)).source_fingerprint, "sha256:source-20260915");

  await assert.rejects(
    bindPipelineTargetDate(db, { runKey, leaseOwner: owner, targetDate: "2026-09-14" }, now),
    /pipeline_window_target_date_conflict/,
  );

  await assert.rejects(
    markPipelineWindow(db, {
      runKey,
      leaseOwner: owner,
      status: "COMPLETED",
      completionAsOf: "2026-09-14",
      publicationStatus: "confirmed",
    }, now),
    /pipeline_window_completion_date_mismatch/,
  );
  const completed = await markPipelineWindow(db, {
    runKey,
    leaseOwner: owner,
    status: "COMPLETED",
    completionAsOf: "2026-09-15",
    publicationStatus: "confirmed",
  }, now);
  assert.equal(completed.status, "COMPLETED");

  await assert.rejects(
    bindPipelineTargetDate(db, { runKey, leaseOwner: owner, targetDate: "2026-09-15" }, now),
    /pipeline_window_target_not_bindable/,
  );
  db.close();
});

test("target-date binding rejects wrong owners, expired leases and malformed identities", async () => {
  const db = await createDatabase();
  const claimedAt = new Date("2026-09-16T00:00:00.000Z");
  const owner = "runner:00000001";
  const runKey = buildPipelineRunKey("2026-09-16", "morning");
  await claimPipelineWindow(db, input(runKey, "morning", owner, { windowDate: "2026-09-16", targetDate: null }), claimedAt);

  await assert.rejects(
    bindPipelineTargetDate(db, {
      runKey,
      leaseOwner: "runner:00000002",
      targetDate: "2026-09-15",
    }, claimedAt),
    /pipeline_window_target_owner_mismatch/,
  );
  await assert.rejects(
    bindPipelineTargetDate(db, {
      runKey,
      leaseOwner: owner,
      targetDate: "2026-09-15",
    }, new Date(claimedAt.getTime() + 15 * 60 * 1_000)),
    /pipeline_window_target_lease_expired/,
  );
  await assert.rejects(
    bindPipelineTargetDate(db, {
      runKey: "daily-market-data:2026-09-16:morning:v2",
      leaseOwner: owner,
      targetDate: "2026-09-15",
    }, claimedAt),
    /invalid_run_key/,
  );
  await assert.rejects(
    bindPipelineTargetDate(db, {
      runKey,
      leaseOwner: owner,
      targetDate: "2026-02-30",
    }, claimedAt),
    /invalid_target_date/,
  );
  db.close();
});

test("concurrent target-date bindings have a single winning date", async () => {
  const db = await createDatabase();
  const now = new Date("2026-09-16T00:00:00.000Z");
  const runKey = buildPipelineRunKey("2026-09-16", "evening");
  await claimPipelineWindow(db, input(runKey, "evening", "runner:00000001", { windowDate: "2026-09-16", targetDate: null }), now);
  const results = await Promise.all([
    bindPipelineTargetDate(db, { runKey, leaseOwner: "runner:00000001", targetDate: "2026-09-15" }, now),
    bindPipelineTargetDate(db, { runKey, leaseOwner: "runner:00000001", targetDate: "2026-09-14" }, now),
  ].map(async (promise) => {
    try {
      return await promise;
    } catch (error) {
      return error;
    }
  }));
  assert.equal(results.filter((result) => result && result.decision === "bound").length, 1);
  assert.equal(results.filter((result) => result instanceof Error && /pipeline_window_target_date_conflict/.test(result.message)).length, 1);
  assert.equal((await getPipelineWindow(db, runKey)).target_date, "2026-09-15");
  db.close();
});

test("invalid identities and ownership cannot claim or update", async () => {
  const db = await createDatabase();
  const now = new Date("2026-08-29T00:00:00.000Z");
  const key = buildPipelineRunKey("2026-08-29", "evening");
  await assert.rejects(
    claimPipelineWindow(db, input("daily-market-data:2026-08-29:evening:v2"), now),
    /run_key_mismatch/,
  );
  await assert.rejects(
    claimPipelineWindow(
      db,
      input("daily-market-data:2026-08-29:evening:v2", "evening", "runner:00000001", { contractVersion: "v2" }),
      now,
    ),
    /unknown_contract_version/,
  );
  await claimPipelineWindow(db, input(key), now);
  await assert.rejects(
    markPipelineWindow(db, { runKey: key, leaseOwner: "runner:00000002", status: "RUNNING" }, now),
    /pipeline_window_status_conflict/,
  );
  db.close();
});

test("protected claim and status routes expose bounded machine-readable responses", async () => {
  const worker = await loadWorker();
  const db = await createDatabase({ claimMeta: indexedClaimMeta(2) });
  const env = {
    DB: db,
    INGEST_API_KEY: "test-key",
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const context = { waitUntil() {}, passThroughOnException() {} };
  const runKey = buildPipelineRunKey("2026-08-29", "evening");
  const claimRequest = {
    runKey,
    windowDate: "2026-08-29",
    windowName: "evening",
    contractVersion: "v1",
    targetDate: "2026-08-29",
    leaseOwner: "runner:00000001",
  };

  const unauthenticated = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/claim", {
      method: "POST",
      body: JSON.stringify(claimRequest),
      headers: { "Content-Type": "application/json" },
    }),
    env,
    context,
  );
  assert.equal(unauthenticated.status, 401);

  const claimResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/claim", {
      method: "POST",
      body: JSON.stringify(claimRequest),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(claimResponse.status, 200);
  const claimBody = await claimResponse.json();
  assert.equal(claimBody.decision, "acquired");
  assert.equal(claimBody.rowsWritten, 2);

  const encoded = encodeURIComponent(runKey);
  const statusResponse = await worker.fetch(
    new Request(`http://localhost/api/v1/admin/pipeline-window/status?runKey=${encoded}`, {
      headers: { Authorization: "Bearer test-key" },
    }),
    env,
    context,
  );
  assert.equal(statusResponse.status, 200);
  assert.equal((await statusResponse.json()).status, "DISPATCHED");

  const incompleteMarkResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/status", {
      method: "POST",
      body: JSON.stringify({
        runKey,
        leaseOwner: "runner:00000001",
        status: "COMPLETED",
      }),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(incompleteMarkResponse.status, 422);
  assert.equal((await incompleteMarkResponse.json()).error, "pipeline_window_completion_proof_required");

  const markResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/status", {
      method: "POST",
      body: JSON.stringify({
        runKey,
        leaseOwner: "runner:00000001",
        githubRunId: "12345678",
        status: "COMPLETED",
        completionAsOf: "2026-08-29",
        publicationStatus: "confirmed",
      }),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(markResponse.status, 200);
  assert.equal((await markResponse.json()).status, "COMPLETED");
  assert.equal((await getPipelineWindow(db, runKey)).github_run_id, "12345678");
  db.close();
});

test("target-date route is authenticated and returns an idempotent binding decision", async () => {
  const worker = await loadWorker();
  const db = await createDatabase();
  const env = {
    DB: db,
    INGEST_API_KEY: "test-key",
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const context = { waitUntil() {}, passThroughOnException() {} };
  const runKey = buildPipelineRunKey("2026-09-16", "reconcile");
  const claimRequest = {
    runKey,
    windowDate: "2026-09-16",
    windowName: "reconcile",
    contractVersion: "v1",
    targetDate: null,
    leaseOwner: "runner:00000001",
  };
  const claimResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/claim", {
      method: "POST",
      body: JSON.stringify(claimRequest),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(claimResponse.status, 200);

  const targetRequest = {
    runKey,
    leaseOwner: "runner:00000001",
    targetDate: "2026-09-15",
    sourceFingerprint: "sha256:source-20260915",
  };
  const unauthenticated = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/target", {
      method: "POST",
      body: JSON.stringify(targetRequest),
      headers: { "Content-Type": "application/json" },
    }),
    env,
    context,
  );
  assert.equal(unauthenticated.status, 401);

  const targetResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/target", {
      method: "POST",
      body: JSON.stringify(targetRequest),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(targetResponse.status, 200);
  const targetBody = await targetResponse.json();
  assert.equal(targetBody.decision, "bound");
  assert.equal(targetBody.targetDate, "2026-09-15");
  assert.equal(targetBody.rowsWritten, 1);
  assert.ok(Number.isSafeInteger(targetBody.rowsRead) && targetBody.rowsRead >= 0);

  const repeatedResponse = await worker.fetch(
    new Request("http://localhost/api/v1/admin/pipeline-window/target", {
      method: "POST",
      body: JSON.stringify(targetRequest),
      headers: {
        Authorization: "Bearer test-key",
        "Content-Type": "application/json",
      },
    }),
    env,
    context,
  );
  assert.equal(repeatedResponse.status, 200);
  assert.equal((await repeatedResponse.json()).decision, "already_bound");
  db.close();
});

test("Cloudflare scheduler claims before dispatch and skips a duplicate window", async () => {
  const worker = await loadWorker();
  const db = await createDatabase();
  const dispatched = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    dispatched.push({ url: String(url), options });
    return new Response(null, { status: 204 });
  };
  try {
    const env = {
      DB: db,
      GITHUB_ACTIONS_TOKEN: "github-token",
      INGEST_API_KEY: "test-key",
      PIPELINE_WINDOW_GATE_ENABLED: "1",
      PIPELINE_WINDOW_CONTRACT_VERSION: "v1",
    };
    const controller = {
      cron: "30 10 * * mon-fri",
      scheduledTime: Date.parse("2026-08-29T10:30:00.000Z"),
    };
    const pending = [];
    const context = { waitUntil(promise) { pending.push(promise); }, passThroughOnException() {} };
    await worker.scheduled(controller, env, context);
    await Promise.all(pending.splice(0));
    assert.equal(dispatched.length, 1);
    const body = JSON.parse(dispatched[0].options.body);
    assert.equal(body.inputs.run_key, "daily-market-data:2026-08-29:evening:v1");
    assert.equal(body.inputs.window_name, "evening");
    // The scheduler date is only the window/runKey anchor. The collector
    // binds the verified source snapshot date before publication.
    assert.equal(body.inputs.target_date, "");
    const claimed = await db.prepare(
      "SELECT window_date, target_date FROM pipeline_window WHERE run_key = ? LIMIT 1",
    ).bind("daily-market-data:2026-08-29:evening:v1").first();
    assert.equal(claimed.window_date, "2026-08-29");
    assert.equal(claimed.target_date, null);

    const duplicatePending = [];
    await worker.scheduled(controller, env, {
      waitUntil(promise) { duplicatePending.push(promise); },
      passThroughOnException() {},
    });
    await Promise.all(duplicatePending);
    assert.equal(dispatched.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
    db.close();
  }
});

test("a D1 claim error fails closed without dispatch", async () => {
  const worker = await loadWorker();
  const dispatched = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    dispatched.push(args);
    return new Response(null, { status: 204 });
  };
  try {
    const env = {
      DB: { prepare() { throw new Error("d1 unavailable"); } },
      GITHUB_ACTIONS_TOKEN: "github-token",
      INGEST_API_KEY: "test-key",
      PIPELINE_WINDOW_GATE_ENABLED: "1",
      PIPELINE_WINDOW_CONTRACT_VERSION: "v1",
    };
    const pending = [];
    await worker.scheduled(
      { cron: "30 10 * * mon-fri", scheduledTime: Date.parse("2026-08-29T10:30:00.000Z") },
      env,
      { waitUntil(promise) { pending.push(promise); }, passThroughOnException() {} },
    );
    await Promise.all(pending);
    assert.equal(dispatched.length, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("flag off preserves dispatch without requiring the pipeline-window table", async () => {
  const worker = await loadWorker();
  const dispatched = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    dispatched.push({ url: String(url), options });
    return new Response(null, { status: 204 });
  };
  try {
    const pending = [];
    await worker.scheduled(
      { cron: "30 10 * * mon-fri", scheduledTime: Date.parse("2026-08-29T10:30:00.000Z") },
      { GITHUB_ACTIONS_TOKEN: "github-token", INGEST_API_KEY: "test-key" },
      { waitUntil(promise) { pending.push(promise); }, passThroughOnException() {} },
    );
    await Promise.all(pending);
    assert.equal(dispatched.length, 1);
    assert.equal(JSON.parse(dispatched[0].options.body).ref, "main");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
