import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { checkpointObjectRequest, CHECKPOINT_OBJECT_MAX_BYTES } from "../app/lib/pipeline-checkpoint-object.ts";

const bytes = new TextEncoder().encode('{"bounded":"fixture"}');
const digest = createHash("sha256").update(bytes).digest("hex");
const now = () => new Date("2026-10-02T00:00:00Z");
function fixture() {
  const row = { run_key: "daily-market-data:2026-09-30:reconcile:v1", target_date: "2026-09-29",
    lease_owner: "test-owner", attempt_count: 2, status: "RUNNING", recovery_id: null,
    lease_expires_at: "2026-10-02T00:15:00Z" };
  const calls = [];
  const objects = new Map();
  const db = { prepare(sql) {
    assert.match(sql, /WHERE run_key = \?\s+LIMIT 1/);
    assert.doesNotMatch(sql, /INSERT|UPDATE|DELETE|DROP/);
    return { bind(key) { assert.equal(key, row.run_key); return this; }, async first() { calls.push("d1-get"); return { ...row }; } };
  } };
  const bucket = {
    async put(key, content, options) {
      calls.push("r2-put");
      assert.equal(options.onlyIf.get("If-None-Match"), "*");
      assert.equal(options.sha256, digest);
      if (objects.has(key)) return null;
      objects.set(key, { bytes: content.slice(), metadata: options.customMetadata });
      return {};
    },
    async get(key) {
      calls.push("r2-get");
      const object = objects.get(key);
      if (!object) return null;
      return { size: object.bytes.length, customMetadata: object.metadata,
        body: new ReadableStream({ start(controller) { controller.enqueue(object.bytes); controller.close(); } }) };
    },
  };
  const query = new URLSearchParams({ runKey: row.run_key, targetDate: row.target_date, stage: "core_collection",
    inputSha256: "b".repeat(64), sha256: digest, producerBuildSha: "a".repeat(40), leaseOwner: row.lease_owner, attemptCount: "2" });
  const request = (method = "POST", body = bytes, auth = true) => new Request(`https://flow.xiaoker.com/api/v1/admin/pipeline-window/checkpoint?${query}`, {
    method, ...(method === "POST" ? { body } : {}), headers: auth ? { Authorization: "Bearer test-key" } : {} });
  return { row, calls, objects, bucket, query, request, runtime: { DB: db, RAW_BUCKET: bucket, INGEST_API_KEY: "test-key" } };
}

test("immutable put and exact get reuse existing authority with no D1 writes or scans", async () => {
  const f = fixture();
  const first = await checkpointObjectRequest(f.request(), f.runtime, now);
  assert.equal(first.status, 200);
  const receipt = await first.json();
  assert.equal(receipt.decision, "verified");
  assert.deepEqual(receipt.usage, { d1RowsRead: null, d1RowsWritten: 0, r2ClassA: 1, r2ClassB: 1, r2Bytes: bytes.length });
  assert.equal(f.objects.size, 1);
  assert.equal((await checkpointObjectRequest(f.request(), f.runtime, now)).status, 200);
  const get = await checkpointObjectRequest(f.request("GET"), f.runtime, now);
  assert.equal(get.status, 200);
  assert.equal(get.headers.get("X-Flow-Checkpoint-Sha256"), digest);
  assert.equal(get.headers.get("X-Flow-Checkpoint-Key"), receipt.key);
  assert.deepEqual(new Uint8Array(await get.arrayBuffer()), bytes);
});

test("handoff uses exact immutable namespace and a smaller bounded body", async () => {
  const f = fixture(); f.query.set("stage", "handoff");
  const put = await checkpointObjectRequest(f.request(), f.runtime, now);
  assert.equal(put.status, 200);
  assert.match((await put.json()).key, /\/handoff\//);
  assert.equal((await checkpointObjectRequest(f.request("GET"), f.runtime, now)).status, 200);
  const before = f.calls.filter((call) => call === "r2-put").length;
  assert.equal((await checkpointObjectRequest(f.request("POST", new Uint8Array(16385)), f.runtime, now)).status, 413);
  assert.equal(f.calls.filter((call) => call === "r2-put").length, before);
});

test("auth and malformed references reject before any storage operation", async () => {
  const f = fixture();
  assert.equal((await checkpointObjectRequest(f.request("POST", bytes, false), f.runtime, now)).status, 401);
  f.query.set("stage", "../../other");
  assert.equal((await checkpointObjectRequest(f.request(), f.runtime, now)).status, 422);
  assert.deepEqual(f.calls, []);
});

test("expired, wrong owner/attempt, completed and recovery leases cannot write", async () => {
  for (const patch of [{ lease_owner: "other" }, { attempt_count: 3 }, { status: "COMPLETED" }, { recovery_id: "recovery" }, { lease_expires_at: "2026-10-02T00:00:00Z" }, { target_date: "2026-09-28" }]) {
    const f = fixture(); Object.assign(f.row, patch);
    assert.equal((await checkpointObjectRequest(f.request(), f.runtime, now)).status, 409);
    assert.deepEqual(f.calls, ["d1-get"]);
  }
});

test("oversize and digest mismatch never put", async () => {
  const f = fixture();
  assert.equal((await checkpointObjectRequest(f.request("POST", new Uint8Array(CHECKPOINT_OBJECT_MAX_BYTES + 1)), f.runtime, now)).status, 413);
  assert.equal((await checkpointObjectRequest(f.request("POST", new TextEncoder().encode("wrong")), f.runtime, now)).status, 422);
  assert.equal(f.calls.includes("r2-put"), false);
});

test("corrupt readback or lease loss after write never reports verified", async () => {
  const f = fixture();
  await checkpointObjectRequest(f.request(), f.runtime, now);
  [...f.objects.values()][0].bytes[0] ^= 1;
  assert.equal((await checkpointObjectRequest(f.request(), f.runtime, now)).status, 409);
  const g = fixture();
  const put = g.bucket.put;
  g.bucket.put = async (...args) => { const result = await put(...args); g.row.lease_owner = "takeover"; return result; };
  assert.equal((await checkpointObjectRequest(g.request(), g.runtime, now)).status, 409);
  assert.equal(g.objects.size, 1); // Orphan immutable object is not completion.
});

test("storage failure is unavailable and exact missing object is 404", async () => {
  const f = fixture();
  assert.equal((await checkpointObjectRequest(f.request("GET"), f.runtime, now)).status, 404);
  f.bucket.put = async () => { throw new Error("private provider details"); };
  const result = await checkpointObjectRequest(f.request(), f.runtime, now);
  assert.equal(result.status, 503);
  assert.deepEqual(await result.json(), { error: "checkpoint_transport_unavailable" });
});

test("compiled admin route rejects unauthenticated calls", async () => {
  const url = new URL("../dist/server/index.js", import.meta.url);
  url.searchParams.set("checkpoint-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(url.href);
  const response = await worker.fetch(new Request("http://localhost/api/v1/admin/pipeline-window/checkpoint"),
    { INGEST_API_KEY: "test-key", ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} });
  assert.equal(response.status, 401);
});
