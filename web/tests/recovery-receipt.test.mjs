import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { validateRecoveryReceipt, verifyRecoveryReceipt } from "../app/lib/recovery-receipt.ts";

globalThis.crypto ??= webcrypto;

const EXPECTED = {
  recoveryId: "recovery-20260904-01",
  runKey: "daily-market-data:2026-08-28:reconcile:v1",
  planSha256: "d".repeat(64),
  attemptCount: 2,
};

function receipt(overrides = {}) {
  return {
    schemaVersion: 1,
    campaignId: "recovery-20260904",
    recoveryId: EXPECTED.recoveryId,
    planHash: EXPECTED.planSha256,
    runKey: EXPECTED.runKey,
    attemptCount: EXPECTED.attemptCount,
    dataDate: "2026-08-28",
    market: "CN",
    stage: "dashboard",
    status: "COMPLETED",
    inputObjects: [{
      key: "recovery/input/2026-08-28/dashboard.json",
      sha256: "a".repeat(64),
      sourceDates: ["2026-08-28"],
      sourceFingerprint: "fixture:2026-08-28:dashboard",
    }],
    versions: { schema: "read-model-v1", calculation: "flow-recovery-v1", classification: "sector-v1" },
    producerBuildSha: "b".repeat(64),
    collectorRunId: "collector-20260828-dashboard",
    publishRunId: "publish-20260828-dashboard",
    output: {
      manifestSha256: "c".repeat(64),
      releaseId: "release-2026-08-28-dashboard",
      slot: "dashboard",
      asOf: "2026-08-28",
    },
    qa: { result: "PASS", evidenceKeys: ["recovery/qa/2026-08-28/dashboard.json"] },
    historical: { dateReadable: true, activeBefore: "release-current", activeAfter: "release-current" },
    resources: {
      d1RowsRead: 10,
      d1RowsWritten: 2,
      workerRequests: 2,
      r2ClassA: 2,
      r2ClassB: 3,
      r2Bytes: 100,
      actionsMinutes: 1,
      measuredAt: "2026-09-04T00:00:00.000Z",
    },
    completedAt: "2026-09-04T00:01:00.000Z",
    ...overrides,
  };
}

async function encoded(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = Array.from(new Uint8Array(digest), (entry) => entry.toString(16).padStart(2, "0")).join("");
  return { bytes, sha256 };
}

function bucketFor(bytes, sha256, metadata = {}) {
  return {
    async head() {
      return { size: bytes.byteLength, customMetadata: { sha256, ...metadata } };
    },
    async get() {
      return { async arrayBuffer() { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); } };
    },
  };
}

test("validates exact runKey, attempt, QA, output date and historical evidence", () => {
  const valid = validateRecoveryReceipt(receipt(), EXPECTED);
  assert.equal(valid.status, "COMPLETED");
  assert.equal(valid.output.asOf, "2026-08-28");
  assert.throws(() => validateRecoveryReceipt(receipt({ runKey: "daily-market-data:2026-08-28:bogus:v1" }), EXPECTED), /invalid_receipt_run_key/);
  assert.throws(() => validateRecoveryReceipt(receipt({ output: { ...receipt().output, asOf: "2026-08-27" } }), EXPECTED), /receipt_output_date_mismatch/);
  assert.throws(() => validateRecoveryReceipt(receipt({ qa: { result: "PASS", evidenceKeys: [] } }), EXPECTED), /receipt_pass_evidence_missing/);
});

test("verifies immutable R2 bytes and metadata before accepting a receipt", async () => {
  const payload = await encoded(receipt());
  const key = "market-data/recovery/receipts/recovery-20260904-01/2026-08-28/dashboard/2.json";
  const valid = await verifyRecoveryReceipt(bucketFor(payload.bytes, payload.sha256), key, payload.sha256, EXPECTED);
  assert.equal(valid.recoveryId, EXPECTED.recoveryId);

  await assert.rejects(
    verifyRecoveryReceipt(bucketFor(payload.bytes, "e".repeat(64)), key, payload.sha256, EXPECTED),
    /receipt_object_not_verified/,
  );
  await assert.rejects(
    verifyRecoveryReceipt(bucketFor(payload.bytes, payload.sha256), key, "f".repeat(64), EXPECTED),
    /receipt_object_not_verified/,
  );
  await assert.rejects(
    verifyRecoveryReceipt(bucketFor(new TextEncoder().encode("{}"), payload.sha256), key, payload.sha256, EXPECTED),
    /receipt_sha256_mismatch/,
  );
  await assert.rejects(
    verifyRecoveryReceipt(bucketFor(payload.bytes, payload.sha256), "market-data/recovery/receipts/../escape.json", payload.sha256, EXPECTED),
    /invalid_receipt_key/,
  );
});

test("does not accept a receipt object above the bounded size", async () => {
  const bytes = new Uint8Array(256 * 1024 + 1);
  await assert.rejects(
    verifyRecoveryReceipt(bucketFor(bytes, "a".repeat(64)), "market-data/recovery/receipts/recovery-20260904-01/2026-08-28/dashboard/2.json", "a".repeat(64), EXPECTED),
    /receipt_object_not_verified/,
  );
});
