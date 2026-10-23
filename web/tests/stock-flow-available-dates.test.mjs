import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

async function worker() {
  const url = new URL("../dist/server/index.js", import.meta.url);
  url.searchParams.set("stock-dates-test", `${process.pid}-${Date.now()}`);
  return (await import(url.href)).default;
}

function fixture() {
  const asOf = "2026-10-21";
  const releaseId = `release:${asOf}:fixture`;
  const dates = ["2026-10-19", "2026-10-20", asOf];
  const body = new TextEncoder().encode(JSON.stringify({ asOf, data: { pressure: { asOf, availableDates: dates } } }));
  const sha256 = createHash("sha256").update(body).digest("hex");
  const key = `market-data/releases/${asOf}/${releaseId}/pressure.json`;
  const manifest = { asOf, releaseId, slot: "etf_penetration", artifacts: { pressure: { key, bytes: body.length, sha256 } } };
  const calls = [];
  const bucket = { async get(requested) {
    calls.push(["r2", requested]);
    assert.equal(requested, key);
    return { size: body.length, body: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }),
      arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength),
      customMetadata: { asOf, releaseId } };
  }, list() { throw new Error("unbounded list forbidden"); }, put() { throw new Error("write forbidden"); } };
  const db = { prepare(sql) {
    calls.push(["d1", sql]);
    assert.match(sql, /WHERE active_release\.slot = \?/);
    assert.doesNotMatch(sql, /INSERT|UPDATE|DELETE|DROP/);
    return { bind(slot) { assert.equal(slot, "etf_penetration"); return this; }, async first() {
      return { release_id: releaseId, as_of: asOf, manifest_sha256: "a".repeat(64),
        activated_at: "2026-10-21T10:00:00Z", manifest_json: JSON.stringify(manifest) };
    } };
  } };
  return { asOf, dates, body, manifest, calls, env: { INGEST_API_KEY: "test-key", DB: db, RAW_BUCKET: bucket,
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } } };
}

test("admin exports authenticated, hash-verified active dates with bounded exact reads", async () => {
  const app = await worker();
  const f = fixture();
  const context = { waitUntil() {}, passThroughOnException() {} };
  const request = (auth) => app.fetch(new Request("http://localhost/api/v1/admin/stock-flow-history/available-dates",
    { headers: auth ? { authorization: "Bearer test-key" } : {} }), f.env, context);
  assert.equal((await request(false)).status, 401);
  assert.deepEqual(f.calls, []);
  const result = await request(true);
  assert.equal(result.status, 200);
  assert.deepEqual((await result.json()).availableDates, f.dates);
  assert.equal(f.calls.filter(([kind]) => kind === "r2").length, 1);
  assert.equal(f.calls.filter(([kind]) => kind === "d1").length, 1);

  f.manifest.artifacts.pressure.sha256 = "b".repeat(64);
  assert.equal((await request(true)).status, 503);
  f.manifest.artifacts.pressure.sha256 = createHash("sha256").update(f.body).digest("hex");
  f.manifest.artifacts.pressure.key = "other-prefix/pressure.json";
  assert.equal((await request(true)).status, 503);
});
