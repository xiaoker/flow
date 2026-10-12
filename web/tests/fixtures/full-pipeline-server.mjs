// Bounded localhost integration of the actual built Worker and SQLite.
// Account caps and bucket are fixtures, never provider quota evidence.
import { createServer } from "node:http";
import { Readable } from "node:stream";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { webcrypto, createHash } from "node:crypto";
import { canonicalJson } from "../../app/lib/pipeline-recovery-plan.ts";
import { DIMENSIONS, BUDGET_SCOPE, createSharedBudgetState, sharedBudgetRequest } from "../../app/lib/pipeline-shared-budget.ts";
const RealDate = Date;
const NOW = "2026-10-03T02:00:00Z";
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [NOW])); }
  static now() { return RealDate.parse(NOW); }
};
// Simulate the Access edge locally, retaining the production JWT verifier.
const issuer = "https://full-pipeline-fixture.cloudflareaccess.com";
const audience = "a".repeat(64);
const pair = await webcrypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
  publicExponent: Uint8Array.of(1, 0, 1), hash: "SHA-256" }, true, ["sign", "verify"]);
const jwk = { ...await webcrypto.subtle.exportKey("jwk", pair.publicKey), kid: "fixture-key", use: "sig", alg: "RS256" };
const jwtHeader = Buffer.from(JSON.stringify({ alg: "RS256", kid: "fixture-key", typ: "JWT" })).toString("base64url");
const jwtBody = Buffer.from(JSON.stringify({ iss: issuer, aud: audience, iat: Date.now() / 1000,
  exp: Date.now() / 1000 + 3600 })).toString("base64url");
const signingInput = `${jwtHeader}.${jwtBody}`;
const jwt = `${signingInput}.${Buffer.from(await webcrypto.subtle.sign({ name: "RSASSA-PKCS1-v1_5" },
  pair.privateKey, new TextEncoder().encode(signingInput))).toString("base64url")}`;
const originalFetch = globalThis.fetch;
globalThis.fetch = (url, options) => String(url) === `${issuer}/cdn-cgi/access/certs`
  ? Promise.resolve(Response.json({ keys: [jwk] })) : originalFetch(url, options);
const { default: worker } = await import("../../dist/server/index.js");
const db = new DatabaseSync(":memory:");
for (const migration of ["0005_release_manifest.sql", "0010_fair_jasper_sitwell.sql", "0011_futuristic_namora.sql", "0015_shared_budget_account.sql"])
  db.exec(readFileSync(new URL(`../../drizzle/${migration}`, import.meta.url), "utf8"));
db.exec("ALTER TABLE release_manifest ADD observed_at TEXT");
const runKey = process.env.FLOW_FULL_FIXTURE_RUN_KEY;
const target = process.env.FLOW_FULL_FIXTURE_TARGET;
const owner = process.env.FLOW_FULL_FIXTURE_OWNER;
const plan = process.env.FLOW_FULL_FIXTURE_PLAN ? JSON.parse(process.env.FLOW_FULL_FIXTURE_PLAN) : null;
if (!runKey || !target || !owner) throw new Error("fixture identity required");
db.prepare(`INSERT INTO pipeline_window (run_key,pipeline,window_date,window_name,contract_version,
  target_date,status,lease_owner,lease_expires_at,attempt_count,created_at,updated_at)
  VALUES (?,'daily-market-data',?,'reconcile','v1',?,'RUNNING',?,'2026-10-03T03:00:00Z',1,?,?)`)
  .run(runKey, runKey.split(":")[1], target, owner, NOW, NOW);
const caps = Object.fromEntries(DIMENSIONS.map(d => [d, 1_000_000_000]));
const units = ["rows", "rows", "requests", "minutes", "bytes", "operations", "operations"];
const state = createSharedBudgetState({ epochDay: "2026-10-03", epochMonth: "2026-10", caps, targets: {},
  evidence: Object.fromEntries(DIMENSIONS.map((d, i) => [d, { value: 0, basis: "measured", unit: units[i], measuredAt: NOW, scope: "account" }])),
  measurementTtlSeconds: 3600, reservationTtlSeconds: 1800 });
db.prepare("INSERT INTO pipeline_budget_account VALUES (?,0,?,?)").run(BUDGET_SCOPE, JSON.stringify(state), NOW);
const counters = { http: {}, sql: 0, r2Put: 0, r2Get: 0, r2Head: 0, bytesStored: 0 };
const queryPlans = new Map();
const adapter = { withSession() { return this; }, async batch(statements) {
  if (!Array.isArray(statements) || statements.length > 8) throw new Error("fixture batch limit");
  db.exec("BEGIN IMMEDIATE");
  try {
    const result = statements.map(statement => statement.runSync());
    db.exec("COMMIT");
    return result;
  } catch (error) { db.exec("ROLLBACK"); throw error; }
}, prepare(sql) { return { bind(...args) {
  if (process.env.FLOW_FULL_FIXTURE_QUERY_PLANS === "1" && !queryPlans.has(sql)) {
    if (queryPlans.size >= 128) throw new Error("fixture query-plan envelope exceeded");
    queryPlans.set(sql, db.prepare(`EXPLAIN QUERY PLAN ${sql}`).all(...args).map(row => row.detail));
  }
  return { async all() { counters.sql++; return { results: db.prepare(sql).all(...args), meta: {} }; },
    async first() { counters.sql++; return db.prepare(sql).get(...args) ?? null; },
    runSync() {
      counters.sql++;
      const info = db.prepare(sql).run(...args);
      // Claim/renew require transport metadata. Emulate it using real SQLite
      // logical changes, NOT Cloudflare physical rows/index writes. Budget
      // all() metadata remains absent and total provider usage stays UNKNOWN.
      return { meta: { changes: Number(info.changes), rows_written: Number(info.changes) } };
    }, async run() { return this.runSync(); } };
} }; } };
const objects = new Map();
if (plan) {
  const bytes = Buffer.from(canonicalJson(plan) + "\n");
  const digest = createHash("sha256").update(bytes).digest("hex");
  const key = `market-data/recovery/plans/${plan.recoveryId}-${digest}.json`;
  objects.set(key, { bytes, customMetadata: { sha256: digest } });
  db.prepare(`INSERT INTO pipeline_window_recovery
    (recovery_id,run_key,plan_key,plan_sha256,scope_sha256,expected_attempt_count,
     expected_updated_at,previous_state_json,reason_code,approval_reference,authorized_at,
     expires_at,status,created_at,updated_at)
    VALUES (?,?,?,?,?,1,?,'{}','fixture','fixture',?,?,'ACTIVE',?,?)`)
    .run(plan.recoveryId, runKey, key, plan.planHash, digest, NOW, NOW, plan.deadline, NOW, NOW);
  db.prepare("UPDATE pipeline_window SET recovery_id=? WHERE run_key=?").run(plan.recoveryId, runKey);
}
const bucket = {
  async put(key, content, options = {}) {
    counters.r2Put++;
    if (objects.has(key) && options.onlyIf) return null;
    const bytes = typeof content === "string" ? Buffer.from(content) : Buffer.from(content);
    if (objects.size >= 80 || bytes.length > 2_000_000 || counters.bytesStored + bytes.length > 20_000_000)
      throw new Error("bounded fixture bucket limit");
    counters.bytesStored += bytes.length;
    objects.set(key, { bytes, customMetadata: options.customMetadata });
    return { key };
  },
  async get(key) {
    counters.r2Get++;
    const item = objects.get(key);
    if (!item) return null;
    return { size: item.bytes.length, customMetadata: item.customMetadata,
      body: new Response(item.bytes).body, arrayBuffer: async () => item.bytes };
  },
  async head(key) { counters.r2Head++; const item = objects.get(key); return item ? { size: item.bytes.length, customMetadata: item.customMetadata } : null; },
};
const env = { DB: adapter, RAW_BUCKET: bucket, INGEST_API_KEY: "fixture-only",
  PIPELINE_RECOVERY_ENABLED: plan ? "1" : "0",
  CANDIDATE_ACCESS_ISSUER: issuer, CANDIDATE_ACCESS_AUD: audience,
  ASSETS: { fetch: async () => new Response("missing", { status: 404 }) } };
let count = 0;
const requestLimit = Number(process.env.FLOW_FULL_FIXTURE_REQUEST_LIMIT);
if (!Number.isSafeInteger(requestLimit) || requestLimit < 1 || requestLimit > 1000)
  throw new Error("bounded fixture request envelope required");
let revisionConflictInjected = false;
const server = createServer(async (req, res) => {
  if (++count > requestLimit) { res.writeHead(429); res.end(); return; }
  try {
    const request = new Request(`http://127.0.0.1${req.url}`, { method: req.method, headers: req.headers,
      ...(req.method === "POST" ? { body: Readable.toWeb(req), duplex: "half" } : {}) });
    if (request.headers.get("authorization") !== "Bearer fixture-only") { res.writeHead(401); res.end(); return; }
    if (request.headers.get("cf-access-client-id") === "fixture-id" && request.headers.get("cf-access-client-secret") === "fixture-secret")
      request.headers.set("cf-access-jwt-assertion", jwt);
    const path = new URL(request.url).pathname;
    counters.http[path] = (counters.http[path] ?? 0) + 1;
    if (path === "/fixture/expire-lease" && request.method === "POST") {
      // Fault injection only: expiry is followed by the real claim endpoint.
      // This fixture never installs a replacement owner directly.
      db.prepare("UPDATE pipeline_window SET lease_expires_at='2026-10-03T01:59:59Z' WHERE run_key=?").run(runKey);
      res.writeHead(200); res.end('{}'); return;
    }
    if (path === "/fixture/revoke-approval" && request.method === "POST" && plan) {
      // Local failure injection, never a production administrative action.
      db.prepare("UPDATE pipeline_window_recovery SET status='REVOKED' WHERE recovery_id=?").run(plan.recoveryId);
      res.writeHead(200); res.end('{}'); return;
    }
    if (path.endsWith("/budget") && request.method === "POST"
        && process.env.FLOW_FULL_FIXTURE_REVISION_CONFLICT === "1" && !revisionConflictInjected) {
      // A real concurrent account CAS update, not a fabricated HTTP success.
      // State/holds are unchanged; the submitted revision must be rejected by
      // the production handler and reconciled using the original operation ID.
      db.prepare("UPDATE pipeline_budget_account SET revision=revision+1 WHERE scope=?").run(BUDGET_SCOPE);
      revisionConflictInjected = true;
    }
    const result = path === "/fixture/stats"
      ? Response.json({ ...counters, queryPlans: [...queryPlans.values()], active: db.prepare("SELECT count(*) AS n FROM active_release").get().n,
          releases: db.prepare("SELECT status FROM release_manifest LIMIT 10").all(),
          budgetReservations: JSON.parse(db.prepare("SELECT state_json FROM pipeline_budget_account WHERE scope=? LIMIT 1").get(BUDGET_SCOPE).state_json).reservations })
      : path.endsWith("/budget")
        ? await sharedBudgetRequest(request, env, () => new Date(NOW), caps)
        : await worker.fetch(request, env, { waitUntil() {}, passThroughOnException() {} });
    const responseBytes = Buffer.from(await result.arrayBuffer());
    if (result.status !== 200) console.error("fixture request rejection", { path, status: result.status, body: responseBytes.toString().slice(0, 300) });
    res.writeHead(result.status, Object.fromEntries(result.headers));
    res.end(responseBytes);
  } catch (error) { res.writeHead(500); res.end(JSON.stringify({ error: error.name })); }
});
server.listen(0, "127.0.0.1", () => console.log(server.address().port));
setTimeout(() => { server.close(); db.close(); process.exit(0); }, 60000).unref();
