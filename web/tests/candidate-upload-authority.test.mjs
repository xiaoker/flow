import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { CAPABILITY_IDS, CAPABILITY_CONTRACT_VERSION, ETF_PERIOD_IDS, isReadModelManifest } from "../app/lib/read-model-contract.ts";
import { parseStagedCandidate } from "../app/lib/candidate-read.ts";
import { candidateUploadAuthority, parseCandidateExecution } from "../app/lib/candidate-upload-authority.ts";

const ref = { runKey: "daily-market-data:2026-09-30:reconcile:v1", leaseOwner: "runner-1",
  attemptCount: 2, targetDate: "2026-09-29", producerBuildSha: "a".repeat(40) };
const manifest = { schemaVersion: 4, asOf: ref.targetDate, producerBuildSha: ref.producerBuildSha };
const now = () => new Date("2026-10-04T00:00:00Z");

test("cross-host upload_once has one effect winner and never resets unknown intents", async () => {
  const url = new URL("../dist/server/index.js", import.meta.url); url.searchParams.set("once", String(Date.now()));
  const { default: worker } = await import(url.href);
  const f = fixture(); f.row.lease_expires_at = new Date(Date.now() + 600000).toISOString();
  const body = uploadManifest();
  const unsigned = JSON.stringify(body.manifest);
  const digest = createHash("sha256").update(unsigned).digest("hex");
  body.candidateManifestJson = `{"manifestSha256":"${digest}",${unsigned.slice(1)}`;
  body.manifest = JSON.parse(body.candidateManifestJson);
  body.operation = "upload_once"; body.finalize = false;
  const objects = new Map(); let artifacts = 0; let crash = false; let takeover = false;
  const env = { INGEST_API_KEY: "local-test-key", DB: f.db, RAW_BUCKET: {
    async put(key, content, options) {
      assert.equal(options.onlyIf.get("If-None-Match"), "*");
      if (objects.has(key)) return null;
      const bytes = typeof content === "string" ? Buffer.from(content) : Buffer.from(content);
      if (key.includes("/upload-intents/")) {
        assert.ok(bytes.byteLength <= 4096);
        objects.set(key, { bytes });
        if (takeover) f.row.lease_owner = "other";
      } else {
        artifacts++;
        if (crash) throw new Error("fixture crash after claim");
        objects.set(key, { bytes, customMetadata: options.customMetadata });
      }
      return { key };
    },
    async get(key) {
      const object = objects.get(key);
      if (!object) return null;
      return { size: object.bytes.byteLength, customMetadata: object.customMetadata,
        body: new Response(object.bytes).body, async arrayBuffer() { return object.bytes; } };
    }, async head() { throw new Error("unexpected head"); },
  }, ASSETS: { fetch: async () => new Response("missing", { status: 404 }) } };
  const invoke = (payload = body) => worker.fetch(new Request("http://localhost/api/v1/admin/read-model-import", {
    method: "POST", headers: { Authorization: "Bearer local-test-key" }, body: JSON.stringify(payload),
  }), env, { waitUntil() {}, passThroughOnException() {} });
  const replies = await Promise.all(Array.from({ length: 20 }, () => invoke()));
  assert.equal(replies.filter((r) => r.status === 200).length, 1);
  assert.equal(replies.filter((r) => r.status === 409).length, 19);
  assert.equal(artifacts, 1); assert.equal(f.getSaved(), null);
  // A host with no local journal still cannot replay the effect.
  assert.equal((await invoke()).status, 409); assert.equal(artifacts, 1);
  assert.equal((await invoke({ ...body, finalize: true })).status, 409); assert.equal(artifacts, 1);
  assert.equal((await invoke({ ...body, operation: "inspect_artifact" })).status, 200);
  objects.clear(); crash = true;
  assert.equal((await invoke()).status, 503); assert.equal(artifacts, 2);
  crash = false;
  assert.equal((await invoke()).status, 409); assert.equal(artifacts, 2);
  assert.equal((await invoke({ ...body, operation: "inspect_artifact" })).status, 404);
  objects.clear(); takeover = true;
  assert.equal((await invoke()).status, 409); assert.equal(artifacts, 2);
  f.row.lease_owner = ref.leaseOwner;
  assert.equal((await invoke({ ...body, pipelineExecution: null })).status, 422);
  assert.ok(f.calls.every((call) => call === "read"));
});

test("fenced intermediate inspection reads exact orphan without any writes", async () => {
  const url = new URL("../dist/server/index.js", import.meta.url); url.searchParams.set("inspect", String(Date.now()));
  const { default: worker } = await import(url.href);
  const f = fixture(); f.row.lease_expires_at = new Date(Date.now() + 600000).toISOString();
  const body = uploadManifest();
  const unsigned = JSON.stringify(body.manifest);
  const digest = createHash("sha256").update(unsigned).digest("hex");
  const raw = `{"manifestSha256":"${digest}",${unsigned.slice(1)}`;
  body.manifest = JSON.parse(raw); body.candidateManifestJson = raw;
  body.operation = "inspect_artifact"; body.finalize = false;
  let missing = false; let corrupt = false; let takeover = false; let gets = 0;
  const env = { INGEST_API_KEY: "local-test-key", DB: f.db, RAW_BUCKET: {
    async get(key) {
      gets++; assert.equal(key, body.artifact.key);
      if (takeover) f.row.lease_owner = "other";
      if (missing) return null;
      const content = corrupt ? Buffer.from("xx") : Buffer.from(body.artifact.contentBase64, "base64");
      return { size: content.byteLength, customMetadata: { releaseId: body.manifest.releaseId,
        asOf: body.manifest.asOf, sha256: body.artifact.sha256 }, body: new Response(content).body };
    }, async put() { throw new Error("forbidden write"); }, async head() { throw new Error("forbidden enumeration"); },
  }, ASSETS: { fetch: async () => new Response("missing", { status: 404 }) } };
  const invoke = (payload = body, auth = true) => worker.fetch(new Request("http://localhost/api/v1/admin/read-model-import", {
    method: "POST", headers: auth ? { Authorization: "Bearer local-test-key" } : {}, body: JSON.stringify(payload),
  }), env, { waitUntil() {}, passThroughOnException() {} });
  assert.equal((await invoke(body, false)).status, 401); assert.equal(gets, 0);
  assert.equal((await invoke({ ...body, pipelineExecution: null })).status, 422); assert.equal(gets, 0);
  assert.equal((await invoke({ ...body, finalize: true })).status, 422); assert.equal(gets, 0);
  const success = await invoke(); assert.equal(success.status, 200);
  assert.equal((await success.json()).status, "verified_artifact");
  assert.equal(f.getSaved(), null); assert.ok(f.calls.every((call) => call === "read"));
  missing = true; assert.equal((await invoke()).status, 404); missing = false;
  corrupt = true; assert.equal((await invoke()).status, 409); corrupt = false;
  takeover = true; assert.equal((await invoke()).status, 409);
  assert.equal(f.getSaved(), null);
});
function fixture(overrides = {}) {
  const row = { run_key: ref.runKey, lease_owner: ref.leaseOwner, attempt_count: 2,
    target_date: ref.targetDate, recovery_id: null, status: "RUNNING", lease_expires_at: "2026-10-04T00:15:00Z", ...overrides };
  const calls = []; let saved = null; let onInsert = () => {};
  const db = { prepare(sql) {
    assert.doesNotMatch(sql, /\b(?:UPDATE|DELETE|DROP|active_release)\b/i);
    return { bind(...args) { return {
      async first() {
        calls.push("read");
        if (sql.includes("FROM pipeline_window")) {
          assert.match(sql, /WHERE run_key = \?\s+LIMIT 1/); assert.equal(args[0], ref.runKey); return { ...row };
        }
        assert.match(sql, /WHERE release_id = \? LIMIT 1/); return saved;
      },
      async run() {
        calls.push("insert"); onInsert();
        assert.match(sql, /WHERE EXISTS/); assert.match(sql, /attempt_count = \?/);
        assert.match(sql, /julianday\(lease_expires_at\)/); assert.match(sql, /ON CONFLICT\(release_id\) DO NOTHING/);
        assert.deepEqual(args.slice(7, 11), [ref.runKey, ref.leaseOwner, 2, ref.targetDate]);
        if (row.lease_owner === ref.leaseOwner && row.attempt_count === ref.attemptCount && row.status === "RUNNING") {
          saved ??= { as_of: args[1], manifest_sha256: args[3], manifest_json: args[4], status: "STAGED" };
        }
        return { meta: { rows_written: saved ? 1 : 0 } };
      },
    }; } };
  } };
  return { row, calls, db, authority: candidateUploadAuthority(db, ref, now),
    getSaved() { return saved; },
    saved(value) { saved = value; }, onInsert(value) { onInsert = value; } };
}

test("execution binds real target date independently of window date", () => {
  assert.deepEqual(parseCandidateExecution(ref, manifest, "stage_only"), ref);
  for (const value of [null, {}, { ...ref, extra: true }, { ...ref, attemptCount: 0 },
    { ...ref, targetDate: "2026-02-30" }, { ...ref, runKey: "invalid" }]) {
    assert.throws(() => parseCandidateExecution(value, manifest, "stage_only"), /invalid_candidate_execution/);
  }
  assert.throws(() => parseCandidateExecution(ref, manifest, "active"), /requires_stage_only/);
  assert.throws(() => parseCandidateExecution(ref, { ...manifest, asOf: "2026-09-30" }, "stage_only"), /manifest_mismatch/);
  assert.throws(() => parseCandidateExecution(ref, { ...manifest, producerBuildSha: "b".repeat(40) }, "stage_only"), /manifest_mismatch/);
});

test("built candidate upload preserves signed producer decimal bytes and rejects mismatch before writes", async () => {
  const url = new URL("../dist/server/index.js", import.meta.url); url.searchParams.set("candidate-bytes-test", String(Date.now()));
  const { default: worker } = await import(url.href);
  const f = fixture(); f.row.lease_expires_at = new Date(Date.now() + 600000).toISOString();
  const body = uploadManifest(); body.manifest.coverage = { fixtureRatio: 1 };
  const unsigned = JSON.stringify(body.manifest).replace('"fixtureRatio":1', '"fixtureRatio":1.0');
  const digest = createHash("sha256").update(unsigned).digest("hex");
  const raw = `{"manifestSha256":"${digest}",${unsigned.slice(1)}`;
  body.manifest = JSON.parse(raw); body.candidateManifestJson = raw; body.finalize = true;
  let writes = 0; const objects = new Map();
  const env = { DB: f.db, INGEST_API_KEY: "local-test-key", RAW_BUCKET: {
    async put(key, content, options) {
      writes++; const bytes = typeof content === "string" ? Buffer.from(content) : content;
      objects.set(key, { size: bytes.byteLength, customMetadata: options.customMetadata, async arrayBuffer() { return bytes; } });
    }, async get(key) { return objects.get(key); }, async head() { return { size: 2 }; },
  }, ASSETS: { fetch: async () => new Response("not found", { status: 404 }) } };
  const invoke = (payload) => worker.fetch(new Request("http://localhost/api/v1/admin/read-model-import", {
    method: "POST", headers: { Authorization: "Bearer local-test-key" }, body: JSON.stringify(payload),
  }), env, { waitUntil() {}, passThroughOnException() {} });
  assert.equal((await invoke({ ...body, candidateManifestJson: raw.replace("1.0", "2.0") })).status, 409);
  assert.equal((await invoke({ ...body, candidateManifestJson: "invalid" })).status, 422);
  const missingLease = { ...body }; delete missingLease.pipelineExecution;
  assert.equal((await invoke(missingLease)).status, 422); assert.equal(writes, 0);
  const response = await invoke(body);
  assert.equal(response.status, 200); assert.equal((await response.json()).status, "staged_candidate");
  assert.equal(f.getSaved().manifest_json, raw); assert.equal(f.getSaved().manifest_sha256, digest);
  assert.match(f.getSaved().manifest_json, /"fixtureRatio":1\.0/);
  const row = { ...f.getSaved(), release_id: body.manifest.releaseId, schema_version: 4 };
  assert.deepEqual(await parseStagedCandidate(row), body.manifest);
  await assert.rejects(parseStagedCandidate({ ...row, manifest_json: JSON.stringify(body.manifest) }), /sha256_mismatch/);
  assert.equal(writes, 2);
});

for (const override of [{ lease_owner: "other" }, { attempt_count: 3 }, { target_date: "2026-09-30" },
  { recovery_id: "recovery" }, { status: "COMPLETED" }, { lease_expires_at: "2026-10-04T00:00:30Z" },
  { lease_expires_at: "invalid" }]) {
  test(`invalid lease rejects before effects: ${JSON.stringify(override)}`, async () => {
    const f = fixture(override); let writes = 0;
    await assert.rejects(f.authority.effect(async () => ++writes), /candidate_lease_conflict/);
    assert.equal(writes, 0); assert.deepEqual(f.calls, ["read"]);
  });
}
test("lease loss after immutable effect cannot report success", async () => {
  const f = fixture();
  await assert.rejects(f.authority.effect(async () => { f.row.attempt_count++; }), /candidate_lease_conflict/);
});
test("stage has atomic lease predicate and bounded exact readback", async () => {
  const f = fixture(); const release = { releaseId: "test-release", asOf: ref.targetDate, manifestSha256: "b".repeat(64) };
  assert.equal(await f.authority.stage(release, manifest, JSON.stringify(manifest)), "staged_candidate");
  assert.deepEqual(f.calls, ["read", "insert", "read", "read", "read"]);
  assert.equal(await f.authority.stage(release, manifest, JSON.stringify(manifest)), "staged_candidate");
});
test("takeover between check and insert is denied by conditional insert", async () => {
  const f = fixture(); f.onInsert(() => { f.row.attempt_count = 3; });
  await assert.rejects(f.authority.stage({ releaseId: "x", asOf: ref.targetDate, manifestSha256: "b".repeat(64) }, manifest, "{}"), /candidate_lease_conflict/);
});
test("existing ACTIVE or conflicting candidate cannot be overwritten", async () => {
  for (const status of ["ACTIVE", "STAGED"]) {
    const f = fixture(); f.saved({ status, as_of: ref.targetDate, manifest_sha256: "c".repeat(64), manifest_json: "{}" });
    await assert.rejects(f.authority.stage({ releaseId: "x", asOf: ref.targetDate, manifestSha256: "b".repeat(64) }, manifest, "{}"), /candidate_stage_conflict/);
  }
});

function uploadManifest() {
  const asOf = ref.targetDate; const releaseId = `release:${asOf}:0123456789abcdef`;
  const names = ["bootstrap", "overview", "radar", "rotation", "national", "connect", "margin"];
  const content = Buffer.from("{}"); const sha256 = createHash("sha256").update(content).digest("hex");
  const artifacts = Object.fromEntries(names.map((name) => [name, {
    key: `market-data/releases/${asOf}/${releaseId}/${name}.json`, bytes: content.length, sha256,
  }]));
  const refs = (names) => names.map((name) => ({ name, ...artifacts[name] }));
  const capabilities = Object.fromEntries(CAPABILITY_IDS.map((id) => [id, {
    status: "UNKNOWN", asOf: null, sourceDates: {}, artifactRefs: [], reasonCode: "separate_slot_not_bound", coverage: {},
  }]));
  const local = { etf_daily_core: ["bootstrap", "radar", "rotation"], etf_radar: ["radar"],
    rotation: ["rotation"], national_team: ["national"], connect: ["connect"], margin: ["margin"], etf_periods: ["radar", "rotation"] };
  for (const [id, names] of Object.entries(local)) capabilities[id] = {
    status: "UNKNOWN", asOf: null, sourceDates: {}, artifactRefs: refs(names), reasonCode: "not_proven", coverage: {},
  };
  capabilities.etf_periods.periods = Object.fromEntries(ETF_PERIOD_IDS.map((id) => [id,
    Object.fromEntries(["shares", "estimatedAmount"].map((metric) => [metric, {
      status: "UNKNOWN", sampleCount: 0, availableSampleCount: 0, missingSampleCount: 0, reasonCode: "not_proven",
    }]))]));
  capabilities.etf_periods.coverage = { periodCount: ETF_PERIOD_IDS.length, readyPeriodMetricCount: 0, expectedPeriodMetricCount: ETF_PERIOD_IDS.length * 2 };
  const value = { ...manifest, releaseId, observedAt: `${asOf}T01:00:00.000Z`, slot: "dashboard",
    sourceDates: { snapshot: asOf }, pressureSchemaVersion: 2, stockShardCount: 8, requiredArtifacts: names, artifacts,
    capabilityContract: { version: CAPABILITY_CONTRACT_VERSION, scope: "dashboard", capabilities } };
  assert.equal(isReadModelManifest(value), true);
  return { version: 1, activationMode: "stage_only", manifest: value,
    artifact: { name: "bootstrap", ...artifacts.bootstrap, contentBase64: content.toString("base64") }, pipelineExecution: ref };
}

test("built upload route preserves legacy upload and fences explicit runner effects", async () => {
  const url = new URL("../dist/server/index.js", import.meta.url); url.searchParams.set("upload-authority-test", String(Date.now()));
  const { default: worker } = await import(url.href);
  const f = fixture(); let puts = 0; let current = null;
  // Route tests use a live future lease, while unit tests above use a fixed clock.
  f.row.lease_expires_at = new Date(Date.now() + 600000).toISOString();
  const env = { INGEST_API_KEY: "local-test-key", DB: {
    prepare(sql) {
      assert.match(sql, /FROM pipeline_window/); assert.match(sql, /WHERE run_key = \?\s+LIMIT 1/);
      return { bind() { return { async first() { return { ...f.row }; } }; } };
    },
  }, RAW_BUCKET: {
    async put(_key, bytes, options) {
      puts++; assert.equal(options.onlyIf.get("If-None-Match"), "*");
      const content = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
      current = { size: content.byteLength, customMetadata: options.customMetadata, async arrayBuffer() { return content; } };
    }, async get() { return current; }, async head() { return null; },
  }, ASSETS: { fetch: async () => new Response("not found", { status: 404 }) } };
  const invoke = (body, auth = true) => worker.fetch(new Request("http://localhost/api/v1/admin/read-model-import", {
    method: "POST", headers: auth ? { Authorization: "Bearer local-test-key" } : {}, body: JSON.stringify(body),
  }), env, { waitUntil() {}, passThroughOnException() {} });
  const body = uploadManifest();
  assert.equal((await invoke(body, false)).status, 401); assert.equal(puts, 0);
  assert.equal((await invoke({ ...body, pipelineExecution: null })).status, 422); assert.equal(puts, 0);
  f.row.lease_owner = "other";
  assert.equal((await invoke(body)).status, 409); assert.equal(puts, 0);
  f.row.lease_owner = ref.leaseOwner;
  assert.equal((await invoke(body)).status, 200); assert.equal(puts, 1);
  env.DB = f.db;
  env.RAW_BUCKET.head = async () => ({ size: 2 });
  const finalized = await invoke({ ...body, finalize: true });
  assert.equal(finalized.status, 200);
  assert.equal((await finalized.json()).status, "staged_candidate");
  assert.equal(puts, 3);
  f.row.status = "COMPLETED";
  assert.equal((await invoke(body)).status, 409); assert.equal(puts, 3);
  f.row.status = "RUNNING";
  const savedDb = env.DB;
  env.DB = { prepare() { throw new Error("private-provider-detail"); } };
  const unavailable = await invoke(body);
  assert.equal(unavailable.status, 503);
  assert.deepEqual(await unavailable.json(), { error: "candidate_upload_unavailable" });
  assert.equal(puts, 3); env.DB = savedDb;
  const legacy = { ...body }; delete legacy.pipelineExecution;
  env.RAW_BUCKET.head = async () => null;
  env.RAW_BUCKET.put = async () => { puts++; }; // Old path does not require conditional options or lease access.
  assert.equal((await invoke(legacy)).status, 200); assert.equal(puts, 4);
});
