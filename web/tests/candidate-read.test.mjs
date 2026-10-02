import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import test from "node:test";
import {
  CAPABILITY_CONTRACT_VERSION,
  CAPABILITY_IDS,
  ETF_PERIOD_IDS,
  hasCompleteReadModelManifest,
  isCandidateOnlyReadModelManifest,
  isReadModelManifest,
} from "../app/lib/read-model-contract.ts";
import {
  candidateErrorMessage,
  CandidateReadError,
  CandidateRequestGate,
  readCandidateArtifact,
} from "../app/lib/candidate-read-client.ts";
import { candidateManifestSha256, parseStagedCandidate } from "../app/lib/candidate-read.ts";

const AS_OF = "2026-09-30";
const RELEASE_ID = `release:${AS_OF}:0123456789abcdef`;
const ACCESS_ISSUER = `https://candidate-read-${process.pid}.cloudflareaccess.com`;
const ACCESS_AUDIENCE = "d3ffcc0ae6d15e7e43053d5499c90775b82ca47f5f684747fd77cc3447a3e67e";
const ACCESS_KEY_PAIR = await webcrypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: Uint8Array.of(1, 0, 1), hash: "SHA-256" },
  true,
  ["sign", "verify"],
);
const ACCESS_PUBLIC_JWK = {
  ...await webcrypto.subtle.exportKey("jwk", ACCESS_KEY_PAIR.publicKey),
  kid: "candidate-read-test-key",
  use: "sig",
  alg: "RS256",
};
const sha = (value) => createHash("sha256").update(value).digest("hex");
const hash = (letter) => letter.repeat(64);

async function accessToken() {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", kid: ACCESS_PUBLIC_JWK.kid, typ: "JWT" })).toString("base64url");
  const now = Math.floor(Date.now() / 1000);
  const payload = Buffer.from(JSON.stringify({
    iss: ACCESS_ISSUER,
    aud: ACCESS_AUDIENCE,
    iat: now,
    exp: now + 3600,
  })).toString("base64url");
  const signingInput = `${header}.${payload}`;
  const signature = await webcrypto.subtle.sign(
    { name: "RSASSA-PKCS1-v1_5" },
    ACCESS_KEY_PAIR.privateKey,
    new TextEncoder().encode(signingInput),
  );
  return `${signingInput}.${Buffer.from(signature).toString("base64url")}`;
}

async function withAccessJwks(callback) {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    if (String(input) === `${ACCESS_ISSUER}/cdn-cgi/access/certs`) {
      return Response.json({ keys: [ACCESS_PUBLIC_JWK] }, { headers: { "Cache-Control": "public, max-age=300" } });
    }
    return previousFetch(input, init);
  };
  try {
    return await callback();
  } finally {
    globalThis.fetch = previousFetch;
  }
}

async function accessHeaders(extra = {}) {
  return { ...extra, "Cf-Access-Jwt-Assertion": await accessToken() };
}

function stableJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
}

function buildManifest() {
  const dashboardArtifacts = ["bootstrap", "overview", "radar", "rotation", "national", "connect", "margin"];
  const capabilityArtifacts = {
    etf_daily_core: ["bootstrap", "radar", "rotation"],
    etf_radar: ["radar"], rotation: ["rotation"], national_team: ["national"],
    connect: ["connect"], margin: ["margin"], etf_periods: ["radar", "rotation"],
    etf_penetration: ["radar_flow", "pressure"], stock_search: ["stock_search"],
    stock_detail: Array.from({ length: 8 }, (_, index) => `stock_${index}`),
  };
  const artifacts = Object.fromEntries(dashboardArtifacts.map((name) => [name, {
    key: `market-data/releases/${AS_OF}/${RELEASE_ID}/${name}.json`,
    sha256: hash("a"),
    bytes: 100,
    contentType: "application/json; charset=utf-8",
  }]));
  const refs = (names) => names.map((name) => ({ name, key: artifacts[name].key, sha256: artifacts[name].sha256, bytes: artifacts[name].bytes }));
  const periodMatrix = () => Object.fromEntries(ETF_PERIOD_IDS.map((period) => [period, {
    shares: { status: "UNKNOWN", sampleCount: 0, availableSampleCount: 0, missingSampleCount: 0, reasonCode: "period_not_proven" },
    estimatedAmount: { status: "UNKNOWN", sampleCount: 0, availableSampleCount: 0, missingSampleCount: 0, reasonCode: "period_not_proven" },
  }]));
  const localState = (status, names, overrides = {}) => ({
    status,
    asOf: status === "READY" ? AS_OF : null,
    sourceDates: {},
    artifactRefs: refs(names),
    reasonCode: status === "READY" ? null : "source_not_proven",
    coverage: {},
    ...overrides,
  });
  const capabilities = Object.fromEntries(CAPABILITY_IDS.map((id) => [id, {
    status: "UNKNOWN", asOf: null, sourceDates: {}, artifactRefs: [],
    reasonCode: "separate_slot_not_bound", coverage: {},
  }]));
  capabilities.etf_daily_core = localState("READY", capabilityArtifacts.etf_daily_core, {
    sourceDates: { snapshot: AS_OF, sseEtfAsOf: AS_OF, szseEtfAsOf: AS_OF },
    coverage: { instrumentCount: 1, tradingDays: 24 },
  });
  capabilities.etf_radar = localState("READY", capabilityArtifacts.etf_radar, {
    sourceDates: { sseEtfAsOf: AS_OF, szseEtfAsOf: AS_OF }, coverage: { sampleCount: 1 },
  });
  capabilities.rotation = localState("WAITING_SOURCES", capabilityArtifacts.rotation);
  capabilities.national_team = localState("WAITING_SOURCES", capabilityArtifacts.national_team);
  capabilities.connect = localState("SOURCE_UNAVAILABLE", capabilityArtifacts.connect);
  capabilities.margin = localState("SOURCE_UNAVAILABLE", capabilityArtifacts.margin);
  capabilities.etf_periods = localState("UNKNOWN", capabilityArtifacts.etf_periods, {
    reasonCode: "etf_daily_core_not_ready",
    coverage: { periodCount: ETF_PERIOD_IDS.length, readyPeriodMetricCount: 0, expectedPeriodMetricCount: ETF_PERIOD_IDS.length * 2 },
    periods: periodMatrix(),
  });
  const manifest = {
    schemaVersion: 4,
    slot: "dashboard",
    releaseId: RELEASE_ID,
    asOf: AS_OF,
    observedAt: `${AS_OF}T01:00:00.000Z`,
    sourceDates: { snapshot: AS_OF, sseEtfAsOf: AS_OF, szseEtfAsOf: AS_OF },
    pressureSchemaVersion: 2,
    stockShardCount: 8,
    requiredArtifacts: dashboardArtifacts,
    artifacts,
    capabilityContract: { version: CAPABILITY_CONTRACT_VERSION, scope: "dashboard", capabilities },
    coverage: { etf: { tradingDays: 24, instrumentCount: 1, availablePeriods: ["1d"] } },
  };
  manifest.manifestSha256 = sha(stableJson(manifest));
  return manifest;
}

function rowFor(manifest, overrides = {}) {
  return {
    release_id: manifest.releaseId,
    as_of: manifest.asOf,
    manifest_sha256: manifest.manifestSha256,
    schema_version: 4,
    status: "STAGED",
    manifest_json: stableJson(manifest),
    ...overrides,
  };
}

function signCanonicalManifest(unsignedJson) {
  const manifestSha256 = sha(unsignedJson);
  const insertion = unsignedJson.indexOf('"observedAt"');
  assert.ok(insertion > 0);
  return {
    manifestSha256,
    manifestJson: `${unsignedJson.slice(0, insertion)}"manifestSha256":"${manifestSha256}",${unsignedJson.slice(insertion)}`,
  };
}

function exactCandidateDb(row) {
  const queries = [];
  return {
    queries,
    prepare(sql) {
      queries.push(sql);
      return {
        bind(...values) {
          assert.deepEqual(values, [row.release_id]);
          return this;
        },
        async first() { return row; },
      };
    },
  };
}

test("candidate manifest digest matches canonical producer contract and rejects tampering", async () => {
  const manifest = buildManifest();
  const manifestJson = stableJson(manifest);
  assert.equal(await candidateManifestSha256(manifestJson), manifest.manifestSha256);
  const parsedManifest = JSON.parse(manifestJson);
  assert.equal(isReadModelManifest(parsedManifest), true);
  assert.equal(isCandidateOnlyReadModelManifest(manifest), true);
  assert.equal(hasCompleteReadModelManifest(manifest), true);
  const parsed = await parseStagedCandidate(rowFor(manifest));
  assert.equal(parsed.releaseId, RELEASE_ID);
  await assert.rejects(parseStagedCandidate(rowFor(manifest, { manifest_sha256: hash("f") })), /candidate_manifest_contract_invalid/);
  await assert.rejects(parseStagedCandidate(rowFor(manifest, { status: "ACTIVE" })), /candidate_manifest_identity_invalid/);

  const dateMismatch = rowFor(manifest, { release_id: `release:2026-09-29:${"f".repeat(16)}` });
  await assert.rejects(parseStagedCandidate(dateMismatch), /candidate_manifest_identity_invalid/);
  const impossibleDate = rowFor(manifest, { as_of: "2026-02-30" });
  await assert.rejects(parseStagedCandidate(impossibleDate), /candidate_manifest_identity_invalid/);
});

test("candidate digest preserves Python canonical float spelling instead of reserializing JavaScript numbers", async () => {
  const manifest = buildManifest();
  manifest.coverage.etf.instrumentCount = 1.0;
  delete manifest.manifestSha256;
  const unsignedJson = stableJson(manifest).replace('"instrumentCount":1', '"instrumentCount":1.0');
  const signed = signCanonicalManifest(unsignedJson);
  const signedManifest = { ...manifest, manifestSha256: signed.manifestSha256 };
  const row = rowFor(signedManifest, { manifest_sha256: signed.manifestSha256, manifest_json: signed.manifestJson });
  assert.equal(await candidateManifestSha256(signed.manifestJson), signed.manifestSha256);
  const parsed = await parseStagedCandidate(row);
  assert.equal(parsed.coverage?.etf?.instrumentCount, 1);
});

test("candidate request gate invalidates late work after refresh or selection change", async () => {
  const gate = new CandidateRequestGate();
  const oldRequest = gate.begin();
  const newRequest = gate.begin();
  assert.equal(oldRequest.signal.aborted, true);
  assert.equal(oldRequest.isCurrent(), false);
  assert.equal(newRequest.isCurrent(), true);
  gate.cancel();
  assert.equal(newRequest.signal.aborted, true);
  assert.equal(newRequest.isCurrent(), false);
});

test("candidate artifact reader rejects mixed release identity and surfaces auth failures", async () => {
  const identity = { releaseId: RELEASE_ID, asOf: AS_OF, manifestSha256: hash("a"), slot: "dashboard" };
  const request = new AbortController();
  const payload = { asOf: AS_OF, data: { ok: 1 } };
  const body = Buffer.from(JSON.stringify(payload));
  const descriptor = { sha256: sha(body), bytes: body.length };
  const correctHeaders = {
    "x-flow-release-id": identity.releaseId,
    "x-flow-as-of": identity.asOf,
    "x-flow-manifest-sha256": identity.manifestSha256,
    "x-flow-release-slot": identity.slot,
    "x-flow-release-status": "STAGED",
    "x-flow-artifact-sha256": descriptor.sha256,
    "x-flow-artifact-bytes": String(descriptor.bytes),
  };
  const accepted = await readCandidateArtifact(async (url, init) => {
    assert.match(String(url), /manifestSha256=/);
    assert.equal(init.headers.Authorization, undefined);
    assert.equal(init.credentials, "same-origin");
    return new Response(body, { headers: correctHeaders });
  }, identity, "radar", descriptor, request.signal);
  assert.deepEqual(accepted, payload);

  await assert.rejects(readCandidateArtifact(async () => Response.json(payload, {
    headers: { ...correctHeaders, "x-flow-release-id": "release:2026-09-29:ffffffffffffffff" },
  }), identity, "radar", descriptor, request.signal), (error) => error.code === "candidate_response_identity_mismatch");
  await assert.rejects(readCandidateArtifact(async () => new Response("", { status: 401 }), identity, "radar", descriptor, request.signal),
    (error) => error.code === "unauthorized");
  await assert.rejects(readCandidateArtifact(async () => Response.json({ error: "storage_unavailable" }, { status: 503 }),
    identity, "radar", descriptor, request.signal), (error) => error.code === "storage_unavailable");
  await assert.rejects(readCandidateArtifact(async () => Response.json({ error: "candidate_artifact_unavailable" }, { status: 503 }),
    identity, "radar", descriptor, request.signal), (error) => error.code === "candidate_artifact_unavailable");
  await assert.rejects(readCandidateArtifact(async () => Response.json({ error: "<script>alert(1)</script>" }, { status: 503 }),
    identity, "radar", descriptor, request.signal), (error) => error.code === "candidate_read_http_503");
  await assert.rejects(readCandidateArtifact(async () => new Response(body, {
    headers: { ...correctHeaders, "x-flow-artifact-sha256": hash("f") },
  }), identity, "radar", descriptor, request.signal), (error) => error.code === "candidate_artifact_descriptor_mismatch");
  await assert.rejects(readCandidateArtifact(async () => new Response(Buffer.from(JSON.stringify({ asOf: AS_OF, data: { ok: 2 } })), {
    headers: correctHeaders,
  }), identity, "radar", descriptor, request.signal), (error) => error.code === "candidate_artifact_sha256_mismatch");
  assert.match(candidateErrorMessage(new CandidateReadError("unauthorized")), /Cloudflare Access/);
  assert.match(candidateErrorMessage(new CandidateReadError("storage_unavailable")), /D1 或 R2/);
  assert.match(candidateErrorMessage(new CandidateReadError("candidate_artifact_unavailable")), /R2 中缺失/);
});

test("built candidate route is explicit, authenticated, staged-only, and exact by release id", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("candidate-release-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const manifest = buildManifest();
  const row = rowFor(manifest);
  const db = exactCandidateDb(row);
  const env = {
    CANDIDATE_ACCESS_ISSUER: ACCESS_ISSUER,
    CANDIDATE_ACCESS_AUD: ACCESS_AUDIENCE,
    INGEST_API_KEY: "test-key",
    DB: db,
    RAW_BUCKET: { async get() { throw new Error("manifest lookup must not read R2"); } },
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const context = { waitUntil() {}, passThroughOnException() {} };

  const unauthorized = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(RELEASE_ID)}`), env, context);
  assert.equal(unauthorized.status, 401);

  const legacyBearer = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(RELEASE_ID)}`, {
    headers: { Authorization: "Bearer test-key" },
  }), env, context);
  assert.equal(legacyBearer.status, 401);

  await withAccessJwks(async () => {
    const response = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(RELEASE_ID)}`, {
      headers: await accessHeaders(),
    }), env, context);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "private, no-store, max-age=0");
    const value = await response.json();
    assert.equal(value.status, "STAGED");
    assert.equal(value.releaseId, RELEASE_ID);
    assert.equal(value.asOf, AS_OF);
    assert.equal(value.manifestSha256, manifest.manifestSha256);
  });
  assert.equal(db.queries.length, 1);
  assert.ok(db.queries.every((sql) => /WHERE release_id = \? AND status = 'STAGED'[\s\S]*LIMIT 1/.test(sql)));
  assert.ok(db.queries.every((sql) => !/active_release|SELECT \*/i.test(sql)));
});

test("candidate artifact route requires manifest identity and validates R2 metadata", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("candidate-artifact-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const manifest = buildManifest();
  const row = rowFor(manifest);
  const db = exactCandidateDb(row);
  const descriptor = manifest.artifacts.radar;
  const body = Buffer.from(JSON.stringify({ asOf: AS_OF, data: { etfTerminal: {} } }));
  let objectKey = null;
  const env = {
    CANDIDATE_ACCESS_ISSUER: ACCESS_ISSUER,
    CANDIDATE_ACCESS_AUD: ACCESS_AUDIENCE,
    INGEST_API_KEY: "test-key",
    DB: db,
    RAW_BUCKET: {
      async get(key) {
        objectKey = key;
        return {
          body: new Response(body).body,
          size: descriptor.bytes,
          customMetadata: { asOf: AS_OF, releaseId: RELEASE_ID, sha256: descriptor.sha256 },
          httpMetadata: { contentType: "application/json" },
          httpEtag: '"candidate-radar"',
        };
      },
    },
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const context = { waitUntil() {}, passThroughOnException() {} };
  await withAccessJwks(async () => {
    const missingHash = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(RELEASE_ID)}&artifact=radar`, {
      headers: await accessHeaders(),
    }), env, context);
    assert.equal(missingHash.status, 400);
    assert.equal(objectKey, null);

    const params = new URLSearchParams({ releaseId: RELEASE_ID, manifestSha256: manifest.manifestSha256, artifact: "radar" });
    const response = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?${params}`, {
      headers: await accessHeaders(),
    }), env, context);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-flow-release-id"), RELEASE_ID);
    assert.equal(response.headers.get("x-flow-artifact-sha256"), descriptor.sha256);
    assert.equal(response.headers.get("x-flow-artifact-bytes"), String(descriptor.bytes));
    assert.equal(response.headers.get("x-flow-as-of"), AS_OF);
    assert.equal(response.headers.get("x-flow-manifest-sha256"), manifest.manifestSha256);
    assert.equal(response.headers.get("x-flow-release-status"), "STAGED");
    assert.equal(objectKey, descriptor.key);
  });
});

test("candidate route does not expose storage exception details", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("candidate-error-sanitization-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const env = {
    CANDIDATE_ACCESS_ISSUER: ACCESS_ISSUER,
    CANDIDATE_ACCESS_AUD: ACCESS_AUDIENCE,
    INGEST_API_KEY: "test-key",
    DB: { prepare() { throw new Error("D1_ERROR: internal table and binding details"); } },
    RAW_BUCKET: { async get() { throw new Error("manifest lookup must not read R2"); } },
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
  const context = { waitUntil() {}, passThroughOnException() {} };
  await withAccessJwks(async () => {
    const response = await worker.fetch(new Request(`http://localhost/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(RELEASE_ID)}`, {
      headers: await accessHeaders(),
    }), env, context);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: "candidate_read_failed" });
  });
});

test("candidate workbench is a separate page and never routes through production active reads", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("candidate-page-test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(new Request("http://localhost/candidate", { headers: { accept: "text/html" } }), {
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, { waitUntil() {}, passThroughOnException() {} });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /候选发布预览/);
  assert.match(html, /STAGED releaseId/);
  assert.match(html, /Cloudflare Access 只读身份/);
  assert.match(html, /本页不收集或发送后台密钥/);
  assert.doesNotMatch(html, /Bearer token|setApiKey|INGEST_API_KEY/);
});
