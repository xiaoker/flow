import assert from "node:assert/strict";
import test from "node:test";
import {
  HistoryArtifactCandidateLimitError,
  listUnindexedHistoryArtifactCandidates,
  MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX,
} from "../app/lib/history-artifact-list.ts";

function item(key, uploaded = "2026-11-02T00:00:00.000Z") {
  return { key, uploaded: new Date(uploaded) };
}

test("unindexed artifact discovery makes one capped list call per exact date prefix", async () => {
  const calls = [];
  const bucket = {
    async list(options) {
      calls.push(options);
      return {
        truncated: false,
        objects: options.prefix.endsWith("2026-09-29/")
          ? [item(`${options.prefix}a/data.csv`), item(`${options.prefix}b/other.csv`)]
          : [item(`${options.prefix}c/data.csv.gz`)],
      };
    },
  };

  const candidates = await listUnindexedHistoryArtifactCandidates(
    bucket,
    ["market-data/normalized/sse/etf_scale/2026-09-29/", "market-data/normalized/sse/etf_scale/20260929/"],
    "data",
  );

  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(({ prefix, limit }) => [prefix, limit]), [
    ["market-data/normalized/sse/etf_scale/2026-09-29/", MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX],
    ["market-data/normalized/sse/etf_scale/20260929/", MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX],
  ]);
  assert.deepEqual(candidates.map(({ key }) => key), [
    "market-data/normalized/sse/etf_scale/2026-09-29/a/data.csv",
    "market-data/normalized/sse/etf_scale/20260929/c/data.csv.gz",
  ]);
});

test("truncated or unproven R2 prefix listing fails closed without following a cursor", async () => {
  for (const reply of [
    { objects: [item("market-data/normalized/szse/etf_scale/2026-10-14/a/data.csv")], truncated: true,
      cursor: "must-not-be-followed" },
    { objects: [item("market-data/normalized/szse/etf_scale/2026-10-14/a/data.csv")] },
    { truncated: false },
    { objects: Array.from({ length: MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX + 1 }, (_, index) =>
      item(`market-data/normalized/szse/etf_scale/2026-10-14/${index}/data.csv`)), truncated: false },
  ]) {
    const calls = [];
    const bucket = { async list(options) { calls.push(options); return reply; } };
    await assert.rejects(
      listUnindexedHistoryArtifactCandidates(
        bucket,
        ["market-data/normalized/szse/etf_scale/2026-10-14/"],
        "data",
      ),
      HistoryArtifactCandidateLimitError,
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0].limit, MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX);
    assert.equal("cursor" in calls[0], false);
  }
});

test("unindexed artifact listing rejects scopes wider than the two date spellings", async () => {
  let calls = 0;
  const bucket = { async list() { calls += 1; return { objects: [], truncated: false }; } };
  await assert.rejects(
    listUnindexedHistoryArtifactCandidates(bucket, ["a/", "b/", "c/"], "data"),
    /invalid_history_artifact_list_scope/,
  );
  assert.equal(calls, 0);
});

test("history artifact route reports a truncated archive search as bounded incomplete evidence", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("bounded-history-list", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const calls = [];
  const runtime = {
    INGEST_API_KEY: "test-key",
    DB: {
      prepare() {
        return { bind() { return { first: async () => null }; } };
      },
    },
    RAW_BUCKET: {
      async list(options) {
        calls.push(options);
        return {
          objects: [item(`${options.prefix}candidate/data.csv`)],
          truncated: true,
          cursor: "must-not-be-followed",
        };
      },
      async get() { throw new Error("truncated evidence must not be read"); },
    },
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const response = await worker.fetch(
    new Request("http://localhost/api/v1/admin/history-artifact?source=sse&dataset=etf_scale&date=2026-09-24", {
      headers: { authorization: "Bearer test-key" },
    }),
    runtime,
    { waitUntil() {}, passThroughOnException() {} },
  );

  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { error: "artifact_candidate_limit_exceeded" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].limit, MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX);
  assert.equal("cursor" in calls[0], false);
});
