import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  parseR2ReleaseMetadataRepairInput,
  repairR2ReleaseMetadata,
} from "../app/lib/r2-release-metadata-repair.ts";

const AS_OF = "2026-08-06";
const RELEASE_ID = "release:2026-08-06:0123456789abcdef";
const ARTIFACT = "overview";
const KEY = `market-data/releases/${AS_OF}/${RELEASE_ID}/${ARTIFACT}.json`;

async function sha256(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function fixture({ metadata = {}, body = "release bytes", bytesOverride, manifestSha } = {}) {
  const encoded = new TextEncoder().encode(body);
  const digest = await sha256(encoded);
  const bytes = bytesOverride ?? encoded.byteLength;
  const manifest = {
    schemaVersion: 2,
    releaseId: RELEASE_ID,
    asOf: AS_OF,
    artifacts: {
      [ARTIFACT]: { key: KEY, sha256: manifestSha ?? digest, bytes },
    },
  };
  const object = {
    key: KEY,
    size: encoded.byteLength,
    etag: "etag-before",
    httpMetadata: { contentType: "application/json" },
    customMetadata: { ...metadata },
  };
  const calls = { first: 0, heads: [], gets: 0, puts: [] };
  const runtime = {
    DB: {
      prepare(sql) {
        return {
          bind(...values) {
            return {
              async first() {
                calls.first += 1;
                assert.match(sql, /FROM release_manifest/);
                assert.deepEqual(values, [AS_OF, RELEASE_ID]);
                return {
                  as_of: AS_OF,
                  release_id: RELEASE_ID,
                  status: "SUPERSEDED",
                  manifest_json: JSON.stringify(manifest),
                };
              },
            };
          },
        };
      },
    },
    RAW_BUCKET: {
      async head(key) {
        calls.heads.push(key);
        return key === KEY ? { ...object, customMetadata: { ...object.customMetadata } } : null;
      },
      async get(key) {
        calls.gets += 1;
        if (key !== KEY) return null;
        return {
          ...object,
          customMetadata: { ...object.customMetadata },
          async arrayBuffer() {
            return encoded.buffer.slice(encoded.byteOffset, encoded.byteOffset + encoded.byteLength);
          },
        };
      },
      async put(key, value, options) {
        calls.puts.push({ key, value: new Uint8Array(value), options });
        object.customMetadata = { ...(options?.customMetadata ?? {}) };
        object.etag = "etag-after";
      },
    },
  };
  return { runtime, calls, manifest, encoded, digest };
}

test("repair input uses the existing release and read-model contracts", () => {
  assert.deepEqual(
    parseR2ReleaseMetadataRepairInput({ asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "dry-run" }),
    { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "dry-run" },
  );
  for (const value of [
    { asOf: "2026-02-30", releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "dry-run" },
    { asOf: AS_OF, releaseId: "release:2026-08-06:bad", artifactName: ARTIFACT, mode: "dry-run" },
    { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: "not-a-view", mode: "dry-run" },
    { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "write" },
  ]) {
    assert.throws(() => parseR2ReleaseMetadataRepairInput(value));
  }
});

test("dry-run verifies bytes but never writes the missing metadata", async () => {
  const value = await fixture({ metadata: { legacy: "kept" } });
  const result = await repairR2ReleaseMetadata(value.runtime, {
    asOf: AS_OF,
    releaseId: RELEASE_ID,
    artifactName: ARTIFACT,
    mode: "dry-run",
  });
  assert.equal(result.status, "verified_missing_metadata");
  assert.equal(result.writePerformed, false);
  assert.equal(result.d1RowsRead, 1);
  assert.equal(result.d1RowsWritten, 0);
  assert.equal(value.calls.gets, 1);
  assert.equal(value.calls.puts.length, 0);
});

test("apply is one conditional same-key write and preserves existing metadata", async () => {
  const value = await fixture({ metadata: { legacy: "kept" } });
  value.runtime.R2_METADATA_REPAIR_ENABLED = "1";
  const result = await repairR2ReleaseMetadata(value.runtime, {
    asOf: AS_OF,
    releaseId: RELEASE_ID,
    artifactName: ARTIFACT,
    mode: "apply",
  });
  assert.equal(result.status, "repaired");
  assert.equal(result.writePerformed, true);
  assert.equal(value.calls.puts.length, 1);
  assert.equal(value.calls.puts[0].key, KEY);
  assert.deepEqual(value.calls.puts[0].options.onlyIf, { etagMatches: "etag-before" });
  assert.equal(value.calls.puts[0].options.customMetadata.legacy, "kept");
  assert.equal(value.calls.puts[0].options.customMetadata.sha256, value.digest);
});

test("already complete metadata avoids body download and write", async () => {
  const value = await fixture({ metadata: { releaseId: RELEASE_ID, asOf: AS_OF } });
  value.runtime.R2_METADATA_REPAIR_ENABLED = "1";
  value.runtime.RAW_BUCKET.head = async (key) => {
    value.calls.heads.push(key);
    return { key, size: value.encoded.byteLength, etag: "etag-before", customMetadata: { releaseId: RELEASE_ID, asOf: AS_OF, sha256: value.digest } };
  };
  const result = await repairR2ReleaseMetadata(value.runtime, {
    asOf: AS_OF,
    releaseId: RELEASE_ID,
    artifactName: ARTIFACT,
    mode: "apply",
  });
  assert.equal(result.status, "already_verified");
  assert.equal(value.calls.gets, 0);
  assert.equal(value.calls.puts.length, 0);
});

test("body hash mismatch, disabled apply, and missing object fail closed", async () => {
  const bad = await fixture({ manifestSha: "a".repeat(64) });
  await assert.rejects(
    repairR2ReleaseMetadata(bad.runtime, { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "dry-run" }),
    /r2_body_hash_mismatch/,
  );
  const disabled = await fixture();
  await assert.rejects(
    repairR2ReleaseMetadata(disabled.runtime, { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "apply" }),
    /repair_disabled/,
  );
  const missing = await fixture();
  missing.runtime.RAW_BUCKET.head = async () => null;
  await assert.rejects(
    repairR2ReleaseMetadata(missing.runtime, { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "dry-run" }),
    /r2_object_missing/,
  );
});

test("a pre-existing conflicting hash is rejected before body download", async () => {
  const value = await fixture({ metadata: { sha256: "b".repeat(64) } });
  value.runtime.R2_METADATA_REPAIR_ENABLED = "1";
  await assert.rejects(
    repairR2ReleaseMetadata(value.runtime, { asOf: AS_OF, releaseId: RELEASE_ID, artifactName: ARTIFACT, mode: "apply" }),
    /r2_hash_mismatch/,
  );
  assert.equal(value.calls.gets, 0);
  assert.equal(value.calls.puts.length, 0);
});

test("route and workflow keep the repair surface explicit and bounded", async () => {
  const route = await readFile(new URL("../app/api/v1/admin/r2-release-metadata-repair/route.ts", import.meta.url), "utf8");
  const workflow = await readFile(new URL("../../.github/workflows/r2-release-metadata-repair.yml", import.meta.url), "utf8");
  assert.match(route, /R2_METADATA_REPAIR_ENABLED/);
  assert.match(route, /x-flow-r2-repair-intent/);
  assert.doesNotMatch(route, /bucket\.list|workflow_dispatch|active_release|DELETE|TRUNCATE/i);
  assert.match(workflow, /workflow_dispatch:/);
  assert.match(workflow, /default: "dry-run"/);
  assert.match(workflow, /MAX_OBJECTS/);
  assert.match(workflow, /MAX_BYTES/);
  assert.doesNotMatch(workflow, /schedule:/);
  assert.doesNotMatch(workflow, /market-data-(collect|publish)|Watchdog|Collector|Publish|PIPELINE_RECOVERY_ENABLED/);
});
