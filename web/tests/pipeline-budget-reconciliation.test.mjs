import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { BUDGET_SCOPE, DIMENSIONS, sharedBudgetRequest, transitionSharedBudget } from "../app/lib/pipeline-shared-budget.ts";
globalThis.crypto ??= webcrypto;
const now = new Date("2026-10-13T02:00:00Z");
const units = ["rows", "rows", "requests", "minutes", "bytes", "operations", "operations"];
const caps = Object.fromEntries(DIMENSIONS.map(d => [d, 10000]));
const evidence = () => Object.fromEntries(DIMENSIONS.map((d, i) => [d, { value: 10, unit: units[i], basis: "measured", scope: "account", measuredAt: now.toISOString() }]));
const payload = () => ({ operation: "account_reconcile", operationId: "a".repeat(64), expectedRevision: null,
  dryRun: true, evidence: evidence(), evidenceReference: `official-usage-sha256:${"d".repeat(64)}` });
function fixture(options = {}) {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../drizzle/0015_shared_budget_account.sql", import.meta.url), "utf8"));
  const sqls = [];
  const adapter = { withSession() { return this; }, prepare(sql) {
    assert(!/DELETE|DROP|REPLACE|pipeline_window/.test(sql)); sqls.push(sql);
    return { bind(...args) { return { async all() {
      if (options.fail) throw new Error("fixture storage failure");
      return { results: db.prepare(sql).all(...args), meta: options.unknownMeta ? {} : { rows_read: 1, rows_written: /^(UPDATE|INSERT)/.test(sql) ? 1 : 0 } };
    } }; } };
  } };
  return { db, sqls, call: (p, token = "test-key", limits = caps) => sharedBudgetRequest(new Request("https://flow.xiaoker.com/api/v1/admin/pipeline-window/budget", {
    method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(p) }),
    { DB: adapter, INGEST_API_KEY: "test-key" }, () => options.clock?.() ?? now, limits),
    row: () => db.prepare("SELECT * FROM pipeline_budget_account WHERE scope=?").get(BUDGET_SCOPE) };
}
test("reconciliation dry-run authenticates, stays read-only, and rejects unknown/stale evidence", async () => {
  const f = fixture();
  assert.equal((await f.call(payload(), "wrong")).status, 401);
  assert.equal(f.sqls.length, 0);
  const reply = await (await f.call(payload())).json();
  assert.equal(reply.decision, "account_reconciliation_prepared");
  assert.equal(reply.executionAllowed, false); assert.equal(reply.rowsWritten, 0);
  assert.equal(f.row(), undefined);
  for (const mutate of [p => { p.evidence.d1RowsRead.value = null; },
    p => { p.evidence.d1RowsRead.basis = "estimated"; },
    p => { p.evidence.workerRequests.measuredAt = "2026-10-12T02:00:00Z"; },
    p => { p.evidence.workerRequests.scope = "task"; },
    p => { p.evidence.workerRequests.measuredAt = "2026-10-14T02:00:00Z"; },
    p => { p.caps = caps; }, p => { delete p.dryRun; }]) {
    const p = payload(); mutate(p); assert.notEqual((await f.call(p)).status, 200);
  }
  assert.equal((await f.call(payload(), "test-key", { ...caps, r2ClassB: null })).status, 409);
  assert.equal(f.row(), undefined); f.db.close();
});

function periodPayload(at = now, cycleStart = "2026-09-16T00:00:00.000Z", cycleEnd = "2026-10-16T00:00:00.000Z") {
  const p = payload(); p.evidenceContract = "flow-account-evidence-v2";
  const day = new Date(at); day.setUTCHours(0, 0, 0, 0);
  const month = new Date(day); month.setUTCDate(1);
  const monthEnd = new Date(month); monthEnd.setUTCMonth(monthEnd.getUTCMonth() + 1);
  for (const d of DIMENSIONS) {
    const m = p.evidence[d]; m.measuredAt = at.toISOString(); m.precision = "exact";
    m.period = d === "r2Bytes" ? { kind: "instant", start: m.measuredAt, end: m.measuredAt }
      : d === "actionsMinutes" ? { kind: "calendar_month", start: month.toISOString(), end: monthEnd.toISOString() }
      : ["r2ClassA", "r2ClassB"].includes(d) ? { kind: "billing_cycle", start: cycleStart, end: cycleEnd }
      : { kind: "utc_day", start: day.toISOString(), end: new Date(day.getTime() + 86400000).toISOString() };
  }
  return p;
}

test("period-aware initialization is explicit, exact and read-only in dry-run", async () => {
  const f = fixture(); const p = periodPayload();
  const r = await (await f.call(p)).json();
  assert.equal(r.decision, "account_reconciliation_prepared");
  assert.equal(r.evidenceContract, "flow-account-evidence-v2");
  assert.equal(r.executionAllowed, false); assert.equal(r.rowsWritten, 0); assert.equal(f.row(), undefined);
  assert.equal((await f.call({ ...p, dryRun: false })).status, 200);
  const stored = JSON.parse(f.row().state_json);
  assert.deepEqual(stored.evidence, p.evidence); assert.equal(stored.evidenceContract, p.evidenceContract);
  const replay = await (await f.call({ ...p, dryRun: false })).json();
  assert.equal(replay.replayed, true); assert.equal(replay.rowsWritten, 0);
  f.db.close();
});

test("wrong periods, rounded labels, stale timestamps and storage GB-months cannot become admission evidence", async () => {
  const f = fixture();
  const cases = [
    p => { p.evidence.r2ClassA.precision = "rounded"; },
    p => { p.evidence.r2ClassA.value = "10.13k"; },
    p => { delete p.evidence.r2ClassA.period; },
    p => { p.evidence.r2ClassA.period.kind = "calendar_month"; },
    p => { p.evidence.r2Bytes.unit = "GB-months"; },
    p => { p.evidence.r2Bytes.period.kind = "billing_cycle"; },
    p => { p.evidence.d1RowsRead.period.start = "2026-10-12T00:00:00.000Z"; },
    p => { p.evidence.actionsMinutes.period.start = "2026-09-16T00:00:00.000Z"; },
    p => { p.evidence.r2ClassB.period.start = "2026-09-17T00:00:00.000Z"; },
    p => { p.evidence.workerRequests.period.start = "2026-10-13T00:00:00+08:00"; },
    p => { p.evidence.workerRequests.measuredAt = "2026-10-13T00:00:00.000Z"; },
    p => { p.evidence.r2ClassA.period.end = "2026-12-16T00:00:00.000Z"; },
    p => { p.evidenceContract = "unknown"; },
  ];
  for (const mutate of cases) {
    const p = periodPayload(); mutate(p); assert.notEqual((await f.call({ ...p, dryRun: false })).status, 200);
    assert.equal(f.row(), undefined);
  }
  f.db.close();
});

test("calendar-month rollover does not reset an unchanged R2 billing cycle", async () => {
  let clock = new Date("2026-09-30T23:59:00Z"); const f = fixture({ clock: () => clock });
  const initial = periodPayload(clock); initial.dryRun = false;
  assert.equal((await f.call(initial)).status, 200);
  clock = new Date("2026-10-01T00:01:00Z");
  const p = { ...periodPayload(clock), dryRun: false, expectedRevision: 0, operationId: "b".repeat(64) };
  p.evidence.d1RowsRead.value = 1; p.evidence.d1RowsWritten.value = 1;
  p.evidence.workerRequests.value = 1; p.evidence.actionsMinutes.value = 1;
  p.evidence.r2ClassA.value = 1;
  assert.equal((await f.call(p)).status, 409); assert.equal(f.row().revision, 0);
  p.evidence.r2ClassA.value = 10;
  assert.equal((await f.call(p)).status, 200); assert.equal(f.row().revision, 1);
  f.db.close();
});

test("R2 mid-month rollover retains UNKNOWN holds, counters and receipts; overlap and downgrade reject", async () => {
  let clock = now; const f = fixture({ clock: () => clock });
  const initial = { ...periodPayload(), dryRun: false };
  assert.equal((await f.call(initial)).status, 200);
  const old = JSON.parse(f.row().state_json);
  const nums = n => Object.fromEntries(DIMENSIONS.map(d => [d, n]));
  old.settled = nums(4); old.reservations.task = { cost: nums(50), consumed: nums(9), authorized: nums(11),
    status: "UNKNOWN", expiresAt: "2026-10-13T03:00:00Z" };
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=? WHERE scope=?").run(JSON.stringify(old), BUDGET_SCOPE);
  const before = f.row();
  assert.equal((await f.call({ ...payload(), dryRun: false, expectedRevision: 0, operationId: "e".repeat(64) })).status, 409);
  const overlap = periodPayload(now, "2026-09-17T00:00:00.000Z", "2026-10-17T00:00:00.000Z");
  assert.equal((await f.call({ ...overlap, dryRun: false, expectedRevision: 0, operationId: "d".repeat(64) })).status, 409);
  assert.deepEqual(f.row(), before);
  clock = new Date("2026-10-16T00:01:00Z");
  const p = { ...periodPayload(clock, "2026-10-16T00:00:00.000Z", "2026-11-16T00:00:00.000Z"),
    dryRun: false, expectedRevision: 0, operationId: "b".repeat(64) };
  for (const d of ["d1RowsRead", "d1RowsWritten", "workerRequests", "r2ClassA", "r2ClassB", "r2Bytes"]) p.evidence[d].value = 1;
  assert.equal((await f.call(p)).status, 200);
  const next = JSON.parse(f.row().state_json);
  assert.deepEqual(next.reservations, old.reservations); assert.deepEqual(next.settled, old.settled);
  assert.deepEqual(next.receipts[initial.operationId], old.receipts[initial.operationId]);
  f.db.close();
});

test("legacy upgrade is explicit and cannot hide lower usage behind invented periods", async () => {
  const f = fixture(); await f.call({ ...payload(), dryRun: false });
  const p = { ...periodPayload(), dryRun: false, expectedRevision: 0, operationId: "b".repeat(64) };
  p.evidence.r2ClassA.value = 1;
  assert.equal((await f.call(p)).status, 409);
  p.evidence.r2ClassA.value = 10;
  assert.equal((await f.call(p)).status, 200);
  assert.equal(JSON.parse(f.row().state_json).evidenceContract, "flow-account-evidence-v2");
  f.db.close();
});

test("period-aware actual reservation uses all seven caps and fails at end-exclusive UTC boundary", async () => {
  const f = fixture(); await f.call({ ...periodPayload(), dryRun: false });
  const state = JSON.parse(f.row().state_json);
  const cost = evidence(); for (const d of DIMENSIONS) cost[d].value = 1;
  const within = transitionSharedBudget(state, "task", { operation: "reserve", vector: cost }, now);
  assert.equal(within.result.decision, "allow");
  for (const d of DIMENSIONS) {
    const over = structuredClone(cost); over[d].value = 10000;
    assert.equal(transitionSharedBudget(state, `large-${d}`, { operation: "reserve", vector: over }, now).result.decision, "over_budget");
  }
  const midnight = new Date("2026-10-14T00:00:00.000Z");
  // Still fresh by timestamp but its UTC-day period ended: must not admit.
  const fresh = periodPayload(new Date("2026-10-13T23:59:59.000Z")); state.evidence = fresh.evidence;
  assert.equal(transitionSharedBudget(state, "tomorrow", { operation: "reserve", vector: cost }, midnight).result.decision, "unknown_metrics");
  const old = within.state.reservations.task; old.status = "UNKNOWN";
  state.reservations.task = old;
  assert.equal(transitionSharedBudget(state, "other", { operation: "reserve", vector: cost }, midnight).result.decision, "unknown_metrics");
  assert.equal(state.reservations.task.status, "UNKNOWN"); f.db.close();
});

test("period-aware concurrent CAS refresh has one winner, and backwards observation cannot overwrite", async () => {
  const f = fixture(); await f.call({ ...periodPayload(), dryRun: false });
  const p = { ...periodPayload(), dryRun: false, expectedRevision: 0 };
  const replies = await Promise.all(Array.from({ length: 20 }, (_, i) => f.call({ ...p, operationId: (500+i).toString(16).padStart(64, "0") })));
  assert.equal(replies.filter(r => r.status === 200).length, 1); assert.equal(f.row().revision, 1);
  const older = periodPayload(new Date(now.getTime() - 1000));
  assert.equal((await f.call({ ...older, dryRun: false, expectedRevision: 1, operationId: "e".repeat(64) })).status, 409);
  assert.equal(f.row().revision, 1); f.db.close();
});

test("R2 monthly cycles accept both 28-day February and 31-day boundaries", async () => {
  for (const [at, start, end] of [
    ["2027-02-20T02:00:00Z", "2027-02-16T00:00:00.000Z", "2027-03-16T00:00:00.000Z"],
    ["2027-02-10T02:00:00Z", "2027-01-16T00:00:00.000Z", "2027-02-16T00:00:00.000Z"],
  ]) {
    const clock = new Date(at); const f = fixture({ clock: () => clock });
    const response = await (await f.call(periodPayload(clock, start, end))).json();
    assert.equal(response.decision, "account_reconciliation_prepared");
    assert.equal(response.rowsWritten, 0); assert.equal(f.row(), undefined); f.db.close();
  }
});

test("expired R2 interval alone rejects admission despite fresh observations and current daily/monthly evidence", async () => {
  const f = fixture(); await f.call({ ...periodPayload(), dryRun: false });
  const state = JSON.parse(f.row().state_json);
  const clock = new Date("2026-10-16T00:00:00.000Z");
  state.epochDay = "2026-10-16";
  state.evidence = periodPayload(clock, "2026-10-16T00:00:00.000Z", "2026-11-16T00:00:00.000Z").evidence;
  const priorCycle = periodPayload(new Date("2026-10-15T23:59:59.000Z")).evidence;
  for (const d of ["r2ClassA", "r2ClassB"]) state.evidence[d] = priorCycle[d];
  const cost = evidence(); for (const d of DIMENSIONS) cost[d].value = 1;
  const result = transitionSharedBudget(state, "next-cycle", { operation: "reserve", vector: cost }, clock);
  assert.equal(result.result.decision, "unknown_metrics");
  assert.deepEqual(result.state.reservations, {}); f.db.close();
});
test("concurrent initialization has one winner; exact replay is read-only", async () => {
  const f = fixture();
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) => f.call({ ...payload(), dryRun: false,
    operationId: i.toString(16).padStart(64, "0") })));
  assert.equal(results.filter(r => r.status === 200).length, 1);
  const winner = results.findIndex(r => r.status === 200);
  const winnerId = winner.toString(16).padStart(64, "0");
  const result = await results[winner].json(); assert.equal(result.executionAllowed, false);
  const row = f.row(); assert.equal(row.revision, 0);
  const replay = await (await f.call({ ...payload(), dryRun: false, operationId: winnerId })).json();
  assert.equal(replay.replayed, true); assert.equal(replay.rowsWritten, 0);
  assert.equal(replay.executionAllowed, false);
  assert.deepEqual(f.row(), row);
  assert.equal((await f.call({ ...payload(), dryRun: false, operationId: winnerId, evidenceReference: `official-usage-sha256:${"e".repeat(64)}` })).status, 409);
  assert.match(f.sqls.find(s => s.startsWith("INSERT")), /ON CONFLICT\(scope\) DO NOTHING/);
  f.db.close();
});

test("existing account dry-run preserves row, prior lower caps and newly lowered policy", async () => {
  const f = fixture(); await f.call({ ...payload(), dryRun: false });
  const old = JSON.parse(f.row().state_json); old.caps.r2ClassA = 100;
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=? WHERE scope=?").run(JSON.stringify(old), BUDGET_SCOPE);
  const before = f.row();
  const p = { ...payload(), expectedRevision: 0, operationId: "b".repeat(64) };
  assert.equal((await (await f.call(p)).json()).decision, "account_reconciliation_prepared");
  assert.deepEqual(f.row(), before);
  assert.equal((await f.call({ ...p, dryRun: false }, "test-key", { ...caps, r2ClassB: 200 })).status, 200);
  const next = JSON.parse(f.row().state_json);
  assert.equal(next.caps.r2ClassA, 100); assert.equal(next.caps.r2ClassB, 200);
  f.db.close();
});

test("true month rollover preserves holds and charges; storage may decrease", async () => {
  const f = fixture(); await f.call({ ...payload(), dryRun: false });
  const old = JSON.parse(f.row().state_json);
  old.epochDay = "2026-09-30"; old.epochMonth = "2026-09";
  const nums = n => Object.fromEntries(DIMENSIONS.map(d => [d, n]));
  old.settled = nums(4); old.reservations.task = { cost: nums(50), consumed: nums(9), authorized: nums(11),
    status: "UNKNOWN", expiresAt: "2026-09-30T03:00:00Z" };
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=? WHERE scope=?").run(JSON.stringify(old), BUDGET_SCOPE);
  const metrics = evidence(); for (const d of DIMENSIONS) metrics[d].value = 1;
  assert.equal((await f.call({ ...payload(), dryRun: false, expectedRevision: 0, operationId: "b".repeat(64), evidence: metrics })).status, 200);
  const next = JSON.parse(f.row().state_json);
  assert.equal(next.epochMonth, "2026-10");
  assert.deepEqual(next.reservations, old.reservations); assert.deepEqual(next.settled, old.settled);
  assert.deepEqual(next.receipts[payload().operationId], old.receipts[payload().operationId]);
  const smallerStorage = structuredClone(metrics); smallerStorage.r2Bytes.value = 0;
  assert.equal((await f.call({ ...payload(), dryRun: false, expectedRevision: 1, operationId: "c".repeat(64), evidence: smallerStorage })).status, 200);
  const dailyRegression = structuredClone(smallerStorage); dailyRegression.d1RowsRead.value = 0;
  assert.equal((await f.call({ ...payload(), dryRun: false, expectedRevision: 2, operationId: "e".repeat(64), evidence: dailyRegression })).status, 409);
  assert.equal(f.row().revision, 2); f.db.close();
});

test("unknown provider metadata stays null; storage errors never create account", async () => {
  const f = fixture({ unknownMeta: true });
  const reply = await (await f.call({ ...payload(), dryRun: false })).json();
  assert.equal(reply.rowsRead, null); assert.equal(reply.rowsWritten, null);
  assert.equal(reply.executionAllowed, false); f.db.close();
  const broken = fixture({ fail: true });
  const error = await broken.call({ ...payload(), dryRun: false });
  assert.equal(error.status, 503); assert.equal((await error.json()).error, "budget_storage_unavailable");
  assert.equal(broken.row(), undefined); broken.db.close();
});

test("receipt capacity rejects without overwriting existing account", async () => {
  const f = fixture(); await f.call({ ...payload(), dryRun: false });
  const state = JSON.parse(f.row().state_json);
  for (let i = 0; i < 127; i++) state.receipts[`fixture-${i}`] = { hash: "f".repeat(64), result: { decision: "held" } };
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=? WHERE scope=?").run(JSON.stringify(state), BUDGET_SCOPE);
  const before = f.row();
  const response = await f.call({ ...payload(), dryRun: false, expectedRevision: 0, operationId: "b".repeat(64) });
  assert.equal(response.status, 409); assert.equal((await response.json()).error, "budget_receipt_capacity");
  assert.deepEqual(f.row(), before); f.db.close();
});
test("refresh and rollover preserve UNKNOWN holds, receipts and all counters", async () => {
  const f = fixture(); await f.call({ ...payload(), dryRun: false });
  const old = JSON.parse(f.row().state_json);
  old.epochDay = "2026-10-12";
  const nums = n => Object.fromEntries(DIMENSIONS.map(d => [d, n]));
  old.settled = nums(4);
  old.reservations.task = { cost: nums(50), consumed: nums(9), authorized: nums(11),
    status: "UNKNOWN", expiresAt: "2026-10-12T03:00:00Z" };
  f.db.prepare("UPDATE pipeline_budget_account SET state_json=? WHERE scope=?").run(JSON.stringify(old), BUDGET_SCOPE);
  const p = { ...payload(), operationId: "b".repeat(64), expectedRevision: 0, dryRun: false };
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) => f.call({ ...p, operationId: (100+i).toString(16).padStart(64, "0") })));
  assert.equal(results.filter(r => r.status === 200).length, 1);
  const next = JSON.parse(f.row().state_json);
  assert.equal(next.epochDay, "2026-10-13"); assert.equal(f.row().revision, 1);
  assert.deepEqual(next.reservations, old.reservations); assert.deepEqual(next.settled, old.settled);
  assert.deepEqual(next.receipts[payload().operationId], old.receipts[payload().operationId]);
  const backward = { ...p, expectedRevision: 1, operationId: "c".repeat(64), evidence: evidence() };
  backward.evidence.r2ClassA.value = 1;
  assert.equal((await f.call(backward)).status, 409);
  assert.equal(f.row().revision, 1);
  const plan = f.db.prepare("EXPLAIN QUERY PLAN SELECT revision,state_json FROM pipeline_budget_account WHERE scope=? LIMIT 1").all(BUDGET_SCOPE);
  assert.match(JSON.stringify(plan), /SEARCH.*INDEX/);
  f.db.close();
});
