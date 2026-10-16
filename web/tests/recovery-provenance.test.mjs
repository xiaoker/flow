import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  applyRecoveryProvenancePlan,
  buildRecoveryProvenancePlan,
  MAX_MANIFEST_BYTES,
  MAX_PROVENANCE_LIMIT,
  parseProvenanceQuery,
  RECOVERY_PROVENANCE_SCOPE_END,
  RECOVERY_PROVENANCE_SCOPE_START,
} from "../app/lib/recovery-provenance.ts";

const SHA = (letter) => letter.repeat(64);
const AS_OF = "2026-08-27";
const SNAPSHOT_KEY = `market-data/history/${AS_OF}/${SHA("c")}.json`;
const MANIFEST_KEY = `market-data/history-manifests/repair-fixture/${AS_OF}.json`;
const RAW_SHA = SHA("a");
const NORMALIZED_SHA = SHA("b");
const NORMALIZED_REVISION = createHash("sha256").update(`${RAW_SHA}:${NORMALIZED_SHA}`).digest("hex");
const RAW_KEY = `market-data/raw/sse/etf_scale/${AS_OF}/${RAW_SHA}.json`;
const METADATA_KEY = `market-data/raw/sse/etf_scale/${AS_OF}/${RAW_SHA}.metadata.json`;
const NORMALIZED_KEY = `market-data/normalized/sse/etf_scale/${AS_OF}/${RAW_SHA}/data.csv`;
const MARGIN_RAW_SHA = SHA("d");
const MARGIN_RAW_KEY = `market-data/raw/sse/margin_market_daily/${AS_OF}/${MARGIN_RAW_SHA}.json`;
const MARGIN_METADATA_KEY = `market-data/raw/sse/margin_market_daily/${AS_OF}/${MARGIN_RAW_SHA}.metadata.json`;
const MARGIN_NORMALIZED_KEY = `market-data/normalized/sse/margin_market_daily/${AS_OF}/${MARGIN_RAW_SHA}/data.csv`;

function artifact({ releaseId = `etf_scale:sse:${AS_OF}:${RAW_SHA.slice(0, 16)}`, observedAt = "2026-08-27T17:00:00+08:00" } = {}) {
  return {
    releaseId,
    sourceId: "sse",
    dataset: "etf_scale",
    requestedDate: AS_OF,
    tradeDates: [AS_OF],
    observedAt,
    rawKey: RAW_KEY,
    metadataKey: METADATA_KEY,
    normalizedKeys: [NORMALIZED_KEY],
    stateKey: "market-data/collection/fixture/2026-08-27/fixture.json",
  };
}

function marginArtifact() {
  return {
    releaseId: `margin_market_daily:sse:${AS_OF}:${MARGIN_RAW_SHA.slice(0, 16)}`,
    sourceId: "sse",
    dataset: "margin_market_daily",
    requestedDate: AS_OF,
    tradeDates: [AS_OF],
    observedAt: "2026-08-27T17:00:00+08:00",
    rawKey: MARGIN_RAW_KEY,
    metadataKey: MARGIN_METADATA_KEY,
    normalizedKeys: [MARGIN_NORMALIZED_KEY],
  };
}

function fixture({ artifactOrder = [artifact()], manifestOverride = {}, missingManifest = false, rawHash = RAW_SHA,
  normalizedIdentity = false, normalizedRawHash = RAW_SHA, gzipIdentity = false,
  normalizedWireHash = gzipIdentity ? SHA("e") : NORMALIZED_SHA } = {}) {
  const expectedRevision = gzipIdentity ? createHash("sha256").update(`${RAW_SHA}:${SHA("e")}`).digest("hex") : NORMALIZED_REVISION;
  const normalizedKey = normalizedIdentity ? NORMALIZED_KEY.replace(RAW_SHA, expectedRevision) + (gzipIdentity ? ".gz" : "") : NORMALIZED_KEY;
  if (normalizedIdentity) artifactOrder = artifactOrder.map((item) => item.dataset === "etf_scale" ? {...item, normalizedKeys:[normalizedKey]} : item);
  const manifest = {
    version: 1,
    runId: "repair-fixture",
    startedAt: "2026-08-27T16:59:00+08:00",
    finishedAt: "2026-08-27T17:01:00+08:00",
    retentionDays: 0,
    asOf: AS_OF,
    snapshotKey: SNAPSHOT_KEY,
    snapshotSha256: SHA("c"),
    artifacts: artifactOrder,
    ...manifestOverride,
  };
  const objects = new Map([
    [MANIFEST_KEY, { size: 1, arrayBuffer: async () => new TextEncoder().encode(JSON.stringify(manifest)).buffer }],
    [SNAPSHOT_KEY, { size: 10, customMetadata: { sha256: SHA("c") } }],
    [RAW_KEY, { size: 10, customMetadata: { sourceId: "sse", dataset: "etf_scale", requestedDate: AS_OF, sha256: rawHash } }],
    [METADATA_KEY, { size: 10, customMetadata: { sha256: rawHash } }],
    [normalizedKey, { size: 20, customMetadata: { sourceId: "sse", dataset: "etf_scale", rowCount: "3", sha256: NORMALIZED_SHA,
      ...(normalizedIdentity ? {normalizedKeyVersion:"normalized-source-wire-sha256-v1",rawSha256:normalizedRawHash,wireSha256:normalizedWireHash} : {}) } }],
    [MARGIN_RAW_KEY, { size: 10, customMetadata: { sourceId: "sse", dataset: "margin_market_daily", requestedDate: AS_OF, sha256: MARGIN_RAW_SHA } }],
    [MARGIN_METADATA_KEY, { size: 10, customMetadata: { sha256: MARGIN_RAW_SHA } }],
    [MARGIN_NORMALIZED_KEY, { size: 20, customMetadata: { sourceId: "sse", dataset: "margin_market_daily", rowCount: "5", sha256: SHA("e") } }],
  ]);
  const calls = { d1Reads: 0, d1Writes: 0, heads: [], gets: 0, writes: [] };
  const runtime = {
    DB: {
      prepare(sql) {
        return {
          bind(...values) {
            return {
              async all() {
                calls.d1Reads += 1;
                assert.match(sql, /FROM historical_snapshot/);
                assert.deepEqual(values, [AS_OF, AS_OF, "", 3]);
                return {
                  results: [{ as_of: AS_OF, observed_at: "2026-08-27T17:02:00Z", snapshot_sha256: SHA("c"), r2_object_key: SNAPSHOT_KEY, manifest_key: missingManifest ? "" : MANIFEST_KEY }],
                  meta: { rows_read: 1, rows_written: 0 },
                };
              },
              async run() {
                calls.d1Writes += 1;
                calls.writes.push({ sql, values });
                return { meta: { rows_written: 1 } };
              },
            };
          },
        };
      },
      async batch(statements) {
        calls.d1Writes += statements.length;
        calls.writes.push(...statements.map((statement) => ({ sql: statement.sql, values: statement.values })));
      },
    },
    RAW_BUCKET: {
      async get(key) {
        calls.gets += 1;
        if (missingManifest || key !== MANIFEST_KEY) return null;
        return objects.get(key);
      },
      async head(key) {
        calls.heads.push(key);
        return objects.get(key) ?? null;
      },
    },
  };
  // D1 statement helpers above keep the SQL and bind values inspectable during
  // apply. This is the shape returned by the Cloudflare adapter as well.
  runtime.DB.prepare = (sql) => ({
    sql,
    values: [],
    bind(...values) {
      this.values = values;
      return this;
    },
    async all() {
      calls.d1Reads += 1;
      assert.match(sql, /FROM historical_snapshot/);
      assert.deepEqual(this.values, [AS_OF, AS_OF, "", 3]);
      return {
        results: [{ as_of: AS_OF, observed_at: "2026-08-27T17:02:00Z", snapshot_sha256: SHA("c"), r2_object_key: SNAPSHOT_KEY, manifest_key: missingManifest ? "" : MANIFEST_KEY }],
        meta: { rows_read: 1, rows_written: 0 },
      };
    },
    async run() {
      calls.d1Writes += 1;
      calls.writes.push({ sql, values: this.values });
      return { meta: { rows_written: 1 } };
    },
  });
  return { runtime, calls, manifest };
}

test("query is fixed to the bounded recovery scope and page cap", () => {
  assert.deepEqual(parseProvenanceQuery(new URLSearchParams()), {
    start: RECOVERY_PROVENANCE_SCOPE_START,
    end: RECOVERY_PROVENANCE_SCOPE_END,
    cursor: null,
    limit: 2,
  });
  assert.throws(() => parseProvenanceQuery(new URLSearchParams(`start=2024-07-31&end=${RECOVERY_PROVENANCE_SCOPE_END}`)), /invalid_provenance_start/);
  assert.throws(() => parseProvenanceQuery(new URLSearchParams(`start=${RECOVERY_PROVENANCE_SCOPE_START}&end=2026-09-04`)), /invalid_provenance_end/);
  assert.throws(() => parseProvenanceQuery(new URLSearchParams(`limit=${MAX_PROVENANCE_LIMIT + 1}`)), /invalid_provenance_limit/);
});

test("valid archive evidence produces deterministic metadata-only plan", async () => {
  const value = fixture();
  const plan = await buildRecoveryProvenancePlan(value.runtime, parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}&limit=2`)));
  assert.equal(plan.status, "READY_TO_APPLY_METADATA_ONLY");
  assert.equal(plan.executionBlocked, true);
  assert.equal(plan.budgetStatus, "unknown");
  assert.equal(plan.readinessRows.length, 1);
  assert.equal(plan.pointerRows.length, 1);
  const sseEtf = plan.stageEvidence.find((item) => item.domain === "sse_etf" && item.dataset === "etf_scale");
  assert.equal(sseEtf?.raw, "READY");
  assert.equal(sseEtf?.model, "UNKNOWN");
  assert.equal(plan.r2.listCalls, 0);
  assert.equal(plan.r2.rawBodiesRead, 0);
  assert.equal(plan.r2.normalizedBodiesRead, 0);
  assert.equal(value.calls.d1Writes, 0);
  assert.equal(value.calls.gets, 1);
  assert.ok(value.calls.heads.includes(RAW_KEY));
  assert.ok(value.calls.heads.includes(NORMALIZED_KEY));
});

test("artifact and JSON member ordering does not change canonical plan hash", async () => {
  const first = fixture({ artifactOrder: [artifact(), marginArtifact()] });
  const second = fixture({ artifactOrder: [marginArtifact(), artifact()] });
  const query = parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`));
  const left = await buildRecoveryProvenancePlan(first.runtime, query);
  const right = await buildRecoveryProvenancePlan(second.runtime, query);
  assert.equal(left.planHash, right.planHash);
});

test("versioned ETF content identity keeps raw lineage and pointer revision", async () => {
  const value=fixture({normalizedIdentity:true});
  const plan=await buildRecoveryProvenancePlan(value.runtime,parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
  assert.equal(plan.status,"READY_TO_APPLY_METADATA_ONLY");
  assert.equal(plan.pointerRows[0].revisionId,NORMALIZED_REVISION);
  assert.equal(plan.pointerRows[0].objectKey,NORMALIZED_KEY.replace(RAW_SHA,NORMALIZED_REVISION));
  assert.equal(plan.pointerRows[0].sha256,NORMALIZED_SHA);
  assert.equal(value.calls.d1Writes,0);
});

for (const fault of ["normalizedRawHash","normalizedWireHash"]) {
  test(`versioned ETF identity rejects ${fault} mismatch`,async () => {
    const value=fixture({normalizedIdentity:true,[fault]:SHA("f")});
    const plan=await buildRecoveryProvenancePlan(value.runtime,parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
    assert.equal(plan.status,"INCOMPLETE");
    assert.ok(plan.issues.some((i)=>i.code==="NORMALIZED_KEY_INVALID"));
    assert.equal(plan.pointerRows.length,0);
  });
}

test("versioned gzip identity binds wire hash separately from decoded CSV hash", async () => {
  for (const corrupt of [false, true]) {
    const value = fixture({normalizedIdentity:true, gzipIdentity:true,
      ...(corrupt ? {normalizedWireHash:SHA("f")} : {})});
    const plan = await buildRecoveryProvenancePlan(value.runtime,parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
    assert.equal(plan.status,corrupt ? "INCOMPLETE" : "READY_TO_APPLY_METADATA_ONLY");
    if (!corrupt) {
      assert.equal(plan.pointerRows[0].contentEncoding,"gzip");
      assert.equal(plan.pointerRows[0].sha256,NORMALIZED_SHA);
      assert.notEqual(plan.pointerRows[0].revisionId,NORMALIZED_SHA);
    } else assert.ok(plan.issues.some((i)=>i.code==="NORMALIZED_KEY_INVALID"));
  }
});

test("missing archive manifest remains provenance-incomplete and cannot be applied", async () => {
  const value = fixture({ missingManifest: true });
  const plan = await buildRecoveryProvenancePlan(value.runtime, parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
  assert.equal(plan.status, "INCOMPLETE");
  assert.equal(plan.readinessRows.length, 0);
  assert.ok(plan.issues.some((item) => item.code === "ARCHIVE_PROVENANCE_INCOMPLETE"));
  await assert.rejects(applyRecoveryProvenancePlan(value.runtime, plan), /plan_not_applyable/);
  assert.equal(value.calls.d1Writes, 0);
});

test("declared raw hash mismatch is rejected without writing metadata", async () => {
  const value = fixture({ rawHash: SHA("c") });
  const plan = await buildRecoveryProvenancePlan(value.runtime, parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
  assert.equal(plan.status, "INCOMPLETE");
  assert.ok(plan.issues.some((item) => item.code === "RAW_HASH_MISSING_OR_MISMATCH"));
  assert.equal(plan.pointerRows.length, 0);
  assert.equal(value.calls.d1Writes, 0);
});

test("apply writes only guarded readiness and pointer metadata with newer-row protection", async () => {
  const value = fixture();
  const plan = await buildRecoveryProvenancePlan(value.runtime, parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
  const applied = await applyRecoveryProvenancePlan(value.runtime, plan);
  assert.deepEqual(applied, { readinessRowsWritten: 1, pointerRowsWritten: 1, d1RowsWritten: 2 });
  assert.equal(value.calls.d1Writes, 2);
  assert.equal(value.calls.writes.some(({ sql }) => /DELETE|TRUNCATE|active_release|data_release|etf_daily|market_history_daily/i.test(sql)), false);
  assert.ok(value.calls.writes.every(({ sql }) => /ON CONFLICT/.test(sql) && /excluded\.observed_at >=/.test(sql)));
});

test("oversized manifest is rejected before body parsing", async () => {
  const value = fixture();
  value.runtime.RAW_BUCKET.get = async () => ({ size: MAX_MANIFEST_BYTES + 1 });
  const plan = await buildRecoveryProvenancePlan(value.runtime, parseProvenanceQuery(new URLSearchParams(`start=${AS_OF}&end=${AS_OF}`)));
  assert.equal(plan.status, "INCOMPLETE");
  assert.ok(plan.issues.some((item) => item.code === "MANIFEST_TOO_LARGE"));
  assert.equal(value.calls.heads.length, 0);
});

test("admin route keeps apply disabled by default and has no production side effects", async () => {
  const source = await readFile(new URL("../app/api/v1/admin/recovery-provenance/route.ts", import.meta.url), "utf8");
  assert.match(source, /RECOVERY_PROVENANCE_REPAIR_ENABLED/);
  assert.match(source, /x-flow-recovery-provenance-intent/);
  assert.match(source, /plan_hash_mismatch/);
  assert.doesNotMatch(source, /workflow_dispatch|Watchdog|Collector|Publish|active_release|INSERT INTO (?:data_release|etf_daily|market_history_daily)/i);
});
