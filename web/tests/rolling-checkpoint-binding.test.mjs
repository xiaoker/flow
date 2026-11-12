import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { bindRollingCheckpoint } from "../app/lib/rolling-checkpoint-binding.ts";

const expected = { releaseId: "release:2026-10-30:1234567890abcdef",
  asOf: "2026-10-30", manifestSha256: "a".repeat(64) };
const bytes = new TextEncoder().encode("fixture");
const sha256 = createHash("sha256").update(bytes).digest("hex");

for (const race of [0, 1, 2, 3]) {
  test(`checkpoint binding checks active identity (race ${race})`, async () => {
    let reads = 0;
    const writes = [];
    const action = bindRollingCheckpoint({
      expected, bytes, sha256,
      readActiveDashboard: async () => ++reads === race ? null : expected,
      putImmutable: async (...args) => { writes.push(args); },
    });
    if (race) {
      await assert.rejects(action, /rolling_checkpoint_active_changed/);
      assert.equal(writes.length, race - 1);
    } else {
      const binding = await action;
      assert.equal(writes.length, 2);
      assert.deepEqual(JSON.parse(new TextDecoder().decode(writes[1][1])), binding);
      assert.ok(writes.every(([key]) => key.includes(expected.releaseId)));
    }
  });
}

test("invalid digest never reads or writes", async () => {
  await assert.rejects(bindRollingCheckpoint({ expected, bytes, sha256: "b".repeat(64),
    readActiveDashboard: async () => assert.fail("unexpected read"),
    putImmutable: async () => assert.fail("unexpected write"),
  }), /rolling_checkpoint_hash_mismatch/);
});
test("metered cold-start checkpoint absence preserves actual D1 reads", async () => {
  const { default: worker } = await import(new URL("../dist/server/index.js", import.meta.url).href);
  for (const count of [3, undefined]) {
    let queries = 0;
    const response = await worker.fetch(new Request("http://localhost/api/v1/admin/etf-state?kind=rolling-checkpoint&metrics=1", {
      headers: { authorization: "Bearer test-key" },
    }), {
      INGEST_API_KEY: "test-key",
      DB: { prepare() { return { async all() {
        queries += 1;
        return { results: [], meta: count === undefined ? {} : { rows_read: count } };
      } }; } },
      RAW_BUCKET: { get() { throw new Error("cold start must not read R2"); } },
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
    }, { waitUntil() {}, passThroughOnException() {} });
    assert.equal(response.status, 404);
    assert.equal(queries, 1);
    assert.equal(response.headers.get("x-flow-d1-rows-read"), count === undefined ? "unknown" : "3");
    assert.equal(response.headers.get("x-flow-r2-class-b"), "0");
  }
});
