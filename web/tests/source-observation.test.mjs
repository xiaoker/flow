import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  buildSourceObservationReceipt,
  isSourceObservationStorageError,
  recordSourceObservation,
  sha256Hex,
} from "../app/lib/source-observation.ts";

globalThis.crypto ??= webcrypto;

const TARGET_DATE = "2026-09-13";
const RUN_KEY = `daily-market-data:${TARGET_DATE}:evening:v1`;

class FakeBucket {
  constructor({ failHead = false, failPut = false } = {}) {
    this.objects = new Map();
    this.putCount = 0;
    this.failHead = failHead;
    this.failPut = failPut;
  }

  async head(key) {
    if (this.failHead) throw new Error("r2_head_transport_detail");
    const object = this.objects.get(key);
    return object ? { size: object.body.byteLength, customMetadata: object.metadata } : null;
  }

  async put(key, body, options = {}) {
    this.putCount += 1;
    if (this.failPut) throw new Error("r2_put_transport_detail");
    const bytes = typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(body);
    this.objects.set(key, {
      body: bytes,
      metadata: options.customMetadata ?? {},
    });
  }
}

class FakeD1 {
  constructor({ fail = false } = {}) {
    this.fail = fail;
    this.rows = new Map();
    this.statements = 0;
  }

  prepare(sql) {
    assert.match(sql, /INSERT INTO source_observation/);
    return {
      bind: (...values) => ({
        run: async () => {
          this.statements += 1;
          if (this.fail) throw new Error("d1_temporarily_unavailable");
          const fingerprint = values[16];
          if (this.rows.has(fingerprint)) return { meta: { changes: 0 } };
          this.rows.set(fingerprint, values);
          return { meta: { changes: 1 } };
        },
      }),
    };
  }
}

async function artifact(overrides = {}) {
  const bytes = new TextEncoder().encode("official-source-body");
  return {
    sourceId: "sse",
    dataset: "etf_scale",
    targetDate: TARGET_DATE,
    requestedDate: TARGET_DATE,
    tradeDates: [TARGET_DATE],
    observedAt: "2026-09-13T09:00:00+08:00",
    sourceUrl: "https://query.sse.com.cn/commonQuery.do",
    dateBasis: "sse_official_trade_date",
    sha256: await sha256Hex(bytes),
    contentType: "application/json",
    rawKey: `market-data/raw/sse/etf_scale/${TARGET_DATE}/${await sha256Hex(bytes)}.json`,
    rawBytes: bytes.byteLength,
    metadata: {
      status_code: 200,
      request_params: { date: TARGET_DATE, api_key: "do-not-persist" },
    },
    releaseId: `etf_scale:sse:${TARGET_DATE}:${(await sha256Hex(bytes)).slice(0, 16)}`,
    archiveRunId: "archive-run-1",
    runKey: RUN_KEY,
    ...overrides,
  };
}

test("same source bytes produce a stable fingerprint across observation times", async () => {
  const first = await buildSourceObservationReceipt(await artifact());
  const second = await buildSourceObservationReceipt(await artifact({
    observedAt: "2026-09-13T09:10:00+08:00",
    archiveRunId: "archive-run-2",
  }));
  assert.equal(first.sourceFingerprint, second.sourceFingerprint);
  assert.equal(first.observationId, second.observationId);
  assert.equal(first.status, "OBSERVED");
  assert.equal(first.sourceBusinessDate, TARGET_DATE);
  assert.deepEqual(first.requestParams, { api_key: "[REDACTED]", date: TARGET_DATE });
  assert.equal(first.createdAt, first.observedAt);
});

test("replaying a receipt is idempotent in both R2 and D1", async () => {
  const bucket = new FakeBucket();
  const db = new FakeD1();
  const input = await artifact();
  const first = await recordSourceObservation(bucket, db, input);
  const second = await recordSourceObservation(bucket, db, input);
  assert.equal(first.r2Status, "created");
  assert.equal(second.r2Status, "existing");
  assert.equal(bucket.putCount, 1);
  assert.equal(db.rows.size, 1);
  assert.equal(first.d1Status, "recorded");
  assert.equal(second.d1Status, "already_recorded");

  const laterReplay = await recordSourceObservation(bucket, db, {
    ...input,
    observedAt: "2026-09-13T09:10:00+08:00",
  });
  assert.equal(laterReplay.r2Status, "existing");
  assert.equal(bucket.putCount, 1);
});

test("D1 failure keeps the immutable R2 receipt and is reported as unavailable", async () => {
  const bucket = new FakeBucket();
  const result = await recordSourceObservation(bucket, new FakeD1({ fail: true }), await artifact());
  assert.equal(result.d1Status, "unavailable");
  assert.equal(result.d1ErrorCode, "source_observation_d1_unavailable");
  assert.equal(result.r2Status, "created");
  assert.equal(bucket.objects.size, 1);
  assert.equal(bucket.putCount, 1);
});

test("R2 head failure is a bounded sidecar error without leaking provider details", async () => {
  await assert.rejects(
    recordSourceObservation(new FakeBucket({ failHead: true }), new FakeD1(), await artifact()),
    (error) => {
      assert.equal(isSourceObservationStorageError(error), true);
      assert.equal(error.code, "source_observation_r2_head_failed");
      assert.doesNotMatch(error.message, /transport_detail/);
      return true;
    },
  );
});

test("R2 put failure is distinguishable from receipt validation and conflicts", async () => {
  await assert.rejects(
    recordSourceObservation(new FakeBucket({ failPut: true }), new FakeD1(), await artifact()),
    (error) => {
      assert.equal(isSourceObservationStorageError(error), true);
      assert.equal(error.code, "source_observation_r2_put_failed");
      assert.doesNotMatch(error.message, /transport_detail/);
      return true;
    },
  );
});

test("receipt identity conflicts remain hard failures", async () => {
  const bucket = new FakeBucket();
  const input = await artifact();
  await recordSourceObservation(bucket, new FakeD1(), input);
  const [key] = bucket.objects.keys();
  bucket.objects.get(key).metadata.sourceFingerprint = "a".repeat(64);
  await assert.rejects(
    recordSourceObservation(bucket, new FakeD1(), input),
    /source_observation_receipt_conflict/,
  );
});

test("source publication states preserve an explicit unavailable reason", async () => {
  const notPublished = await buildSourceObservationReceipt(await artifact({
    tradeDates: [],
    metadata: { status_code: 404 },
  }));
  assert.equal(notPublished.status, "NOT_PUBLISHED");
  assert.equal(notPublished.reasonCode, "source_not_published");

  const unavailable = await buildSourceObservationReceipt(await artifact({
    tradeDates: [],
    metadata: { status_code: 503 },
  }));
  assert.equal(unavailable.status, "SOURCE_UNAVAILABLE");
  assert.equal(unavailable.reasonCode, "source_http_error");
});

test("invalid pipeline run keys are rejected before any storage call", async () => {
  await assert.rejects(
    buildSourceObservationReceipt(await artifact({ runKey: "github-run-123" })),
    /source_observation_invalid_run_key/,
  );
});

test("shadow integration is opt-in and does not touch the active pointer", async () => {
  const route = await readFile(new URL("../app/api/v1/admin/history-import/route.ts", import.meta.url), "utf8");
  const wrangler = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  assert.match(route, /SOURCE_OBSERVATION_SHADOW_ENABLED/);
  assert.match(route, /recordSourceObservation/);
  assert.match(route, /if \(!isSourceObservationStorageError\(error\)\) throw error/);
  assert.match(route, /sourceObservationR2Unavailable/);
  assert.match(route, /sourceObservationStatus/);
  assert.match(route, /return "disabled"/);
  assert.match(route, /return d1Unavailable > 0 \|\| r2Unavailable > 0 \? "degraded" : "recorded"/);
  assert.doesNotMatch(route, /source_observation_receipt_failed/);
  assert.doesNotMatch(route, /UPDATE\s+active_release/i);
  assert.match(wrangler, /"SOURCE_OBSERVATION_SHADOW_ENABLED"\s*:\s*"0"/);
});
