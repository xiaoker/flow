// Localhost-only cross-language fixture. No production credentials or remote IO.
import { createServer } from "node:http";
import { Readable } from "node:stream";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { canonicalJson } from "../../app/lib/pipeline-recovery-plan.ts";
import { DIMENSIONS, BUDGET_SCOPE, createSharedBudgetState, sharedBudgetRequest } from "../../app/lib/pipeline-shared-budget.ts";
const db = new DatabaseSync(":memory:");
db.exec(readFileSync(new URL("../../drizzle/0015_shared_budget_account.sql", import.meta.url), "utf8"));
db.exec("CREATE TABLE pipeline_window (run_key TEXT PRIMARY KEY,lease_owner TEXT,attempt_count INTEGER,target_date TEXT,recovery_id TEXT,status TEXT,lease_expires_at TEXT)");
db.prepare("INSERT INTO pipeline_window VALUES ('daily-market-data:2026-10-03:reconcile:v1','owner',1,'2026-10-02',NULL,'RUNNING','2026-10-03T03:00:00Z')").run();
const plan = process.env.FLOW_RECOVERY_FIXTURE_PLAN ? JSON.parse(process.env.FLOW_RECOVERY_FIXTURE_PLAN) : null;
let bucket;
if (plan) {
  const bytes = new TextEncoder().encode(canonicalJson(plan) + "\n");
  const sha = createHash("sha256").update(bytes).digest("hex");
  const objectKey = `market-data/recovery/plans/${plan.recoveryId}-${sha}.json`;
  db.exec("CREATE TABLE pipeline_window_recovery (recovery_id TEXT PRIMARY KEY,run_key TEXT,plan_key TEXT,plan_sha256 TEXT,scope_sha256 TEXT,status TEXT,expires_at TEXT)");
  db.prepare("INSERT INTO pipeline_window_recovery VALUES (?,?,?,?,?,'ACTIVE','2026-10-03T03:00:00Z')").run(plan.recoveryId, plan.runKey, objectKey, plan.planHash, sha);
  db.prepare("UPDATE pipeline_window SET recovery_id=?").run(plan.recoveryId);
  bucket = { async head(key) { return key === objectKey ? { size: bytes.length, customMetadata: { sha256: sha } } : null; },
    async get(key) { return key === objectKey ? { arrayBuffer: async () => bytes.buffer } : null; },
    async put() { throw new Error("fixture plan mutation forbidden"); } };
}
const units = ["rows", "rows", "requests", "minutes", "bytes", "operations", "operations"];
const state = createSharedBudgetState({ epochDay: "2026-10-03", epochMonth: "2026-10", caps: Object.fromEntries(DIMENSIONS.map(d => [d, 100])), targets: {},
  evidence: Object.fromEntries(DIMENSIONS.map((d, i) => [d, { value: 0, basis: "measured", unit: units[i], measuredAt: "2026-10-03T02:00:00Z", scope: "account" }])),
  measurementTtlSeconds: 3600, reservationTtlSeconds: 1800 });
db.prepare("INSERT INTO pipeline_budget_account VALUES (?,0,?,'2026-10-03T02:00:00Z')").run(BUDGET_SCOPE, JSON.stringify(state));
const adapter = { prepare(sql) { return { bind(...args) { return { async all() {
  return { results: db.prepare(sql).all(...args), meta: {} }; // Physical D1 counters intentionally UNKNOWN.
} }; } }; } };
let requests = 0;
const server = createServer(async (req, res) => {
  if (++requests > 64) { res.writeHead(429); res.end(); return; }
  try {
    const request = new Request(`http://127.0.0.1${req.url}`, { method: req.method, headers: req.headers,
      ...(req.method === "POST" ? { body: Readable.toWeb(req), duplex: "half" } : {}) });
    const result = await sharedBudgetRequest(request, { DB: adapter, INGEST_API_KEY: "fixture-only",
      PIPELINE_RECOVERY_ENABLED: plan ? "1" : "0", RAW_BUCKET: bucket }, () => new Date("2026-10-03T02:00:00Z"), Object.fromEntries(DIMENSIONS.map(d => [d, 100])));
    res.writeHead(result.status, Object.fromEntries(result.headers)); res.end(await result.text());
  } catch { res.writeHead(500); res.end(); }
});
server.listen(0, "127.0.0.1", () => console.log(server.address().port));
setTimeout(() => { server.close(); db.close(); process.exit(0); }, 30000).unref();
