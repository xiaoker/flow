/** Candidate-only account reservation protocol. No scheduler or live runner caller. */
import { parsePipelineRunKey, PipelineWindowError } from "./pipeline-window.ts";
import resourceConfig from "../../../config/resource-budget.json" with { type: "json" };

export const DIMENSIONS = ["d1RowsRead", "d1RowsWritten", "workerRequests", "actionsMinutes", "r2Bytes", "r2ClassA", "r2ClassB"] as const;
const UNITS = ["rows", "rows", "requests", "minutes", "bytes", "operations", "operations"];
export const BUDGET_SCOPE = "flow-account-v1";
export const MAX_STATE_BYTES = 131072;
export const MAX_RESERVATIONS = 64;
export const MAX_RECEIPTS = 128;
type Dimension = typeof DIMENSIONS[number];
type Numbers = Record<Dimension, number>;
type Caps = Record<Dimension, number | null>;
type Metric = { value: number | null; unit: string; basis: string; measuredAt?: string; scope?: string };
type Vector = Record<Dimension, Metric>;
type Reservation = { cost: Numbers; consumed: Numbers; authorized: Numbers; status: string; expiresAt: string; settled?: string };
export type SharedBudgetState = {
  contract: "flow-shared-budget-v1"; epochDay: string; epochMonth: string;
  evidence: Vector; caps: Record<Dimension, number | null>; targets: Partial<Numbers>;
  measurementTtlSeconds: number; reservationTtlSeconds: number;
  settled: Numbers; reservations: Record<string, Reservation>;
  receipts: Record<string, { hash: string; result: Record<string, unknown> }>;
};
type Statement = { bind(...values: unknown[]): Statement; all<T>(): Promise<{ results: T[]; meta?: { rows_read?: number; rows_written?: number; changes?: number } }> };
type DB = { prepare(sql: string): Statement; withSession?(constraint: string): DB };
type Input = { runKey: string; leaseOwner: string; attemptCount: number; targetDate: string; operationId: string; operation: string; expectedRevision: number; vector?: Vector; outcome?: string };
const fail = (code: string, status = 422): never => { throw new PipelineWindowError(code, status); };
const integer = (n: unknown) => typeof n === "number" && Number.isSafeInteger(n) && n >= 0;
const zero = (): Numbers => Object.fromEntries(DIMENSIONS.map(d => [d, 0])) as Numbers;
const add = (a: Numbers, b: Numbers): Numbers => Object.fromEntries(DIMENSIONS.map(d => {
  const n = a[d] + b[d]; if (!integer(n)) fail("budget_counter_overflow"); return [d, n];
})) as Numbers;
const exceeds = (a: Numbers, b: Numbers) => DIMENSIONS.some(d => a[d] > b[d]);
const size = (v: unknown) => new TextEncoder().encode(JSON.stringify(v)).byteLength;
// Config remains the single policy source. A stored evidence row cannot raise
// code-configured caps; absent provider limits stay UNKNOWN, not unlimited.
const policyCaps: Caps = {
  d1RowsRead: Math.min(resourceConfig.limits.d1RowsReadPerDay, resourceConfig.hardLimits.d1RowsReadPerDay),
  d1RowsWritten: Math.min(resourceConfig.limits.d1RowsWrittenPerDay, resourceConfig.hardLimits.d1RowsWrittenPerDay),
  workerRequests: Math.min(resourceConfig.limits.workersRequestsPerDay, resourceConfig.hardLimits.workersRequestsPerDay),
  actionsMinutes: Math.min(resourceConfig.limits.githubActionsMinutesPerMonth, resourceConfig.hardLimits.githubActionsMinutesPerMonth),
  r2Bytes: resourceConfig.storage.r2SoftBytes,
  r2ClassA: integer((resourceConfig.limits as Record<string, unknown>).r2ClassA) ? (resourceConfig.limits as Record<string, number>).r2ClassA : null,
  r2ClassB: integer((resourceConfig.limits as Record<string, unknown>).r2ClassB) ? (resourceConfig.limits as Record<string, number>).r2ClassB : null,
};
function numbers(v: Vector | undefined, measured = false): Numbers {
  if (!v || typeof v !== "object" || Object.keys(v).length !== 7) fail("budget_incomplete_vector");
  return Object.fromEntries(DIMENSIONS.map((d, i) => {
    const m = v[d];
    if (!m || m.unit !== UNITS[i] || !integer(m.value) || !["measured", "estimated"].includes(m.basis)
        || (measured && m.basis !== "measured")) fail(measured ? "budget_requires_measured_vector" : "budget_incomplete_vector");
    return [d, m.value];
  })) as Numbers;
}
function validNumbers(v: Numbers) { if (!v || Object.keys(v).length !== 7 || DIMENSIONS.some(d => !integer(v[d]))) fail("budget_state_corrupt", 503); }
function validateState(s: SharedBudgetState) {
  if (!s || s.contract !== "flow-shared-budget-v1" || !/^\d{4}-\d{2}-\d{2}$/.test(s.epochDay)
      || s.epochMonth !== s.epochDay.slice(0, 7) || !integer(s.measurementTtlSeconds) || !s.measurementTtlSeconds || s.measurementTtlSeconds > 3600
      || !integer(s.reservationTtlSeconds) || !s.reservationTtlSeconds || s.reservationTtlSeconds > 1800 || !s.reservations || !s.receipts
      || Object.keys(s.reservations).length > MAX_RESERVATIONS || Object.keys(s.receipts).length > MAX_RECEIPTS
      || size(s) > MAX_STATE_BYTES) fail("budget_state_corrupt", 503);
  validNumbers(s.settled);
  for (const [i, d] of DIMENSIONS.entries()) {
    const m = s.evidence?.[d]; const cap = s.caps?.[d];
    if (!m || m.unit !== UNITS[i] || !["measured", "estimated", "unknown"].includes(m.basis)
        || (m.basis === "unknown" ? m.value !== null : !integer(m.value))
        || !(cap === null || integer(cap)) || (s.targets?.[d] !== undefined && !integer(s.targets[d]))) fail("budget_state_corrupt", 503);
  }
  for (const r of Object.values(s.reservations)) {
    validNumbers(r.cost); validNumbers(r.consumed); validNumbers(r.authorized);
    if (!["RESERVED", "RUNNING", "UNKNOWN", "OVER_BUDGET", "SUCCEEDED", "FAILED_KNOWN"].includes(r.status)
        || !Number.isFinite(Date.parse(r.expiresAt))) fail("budget_state_corrupt", 503);
  }
  for (const r of Object.values(s.receipts)) if (!/^[a-f0-9]{64}$/.test(r.hash) || !r.result || typeof r.result.decision !== "string") fail("budget_state_corrupt", 503);
}
/** Offline provisioning seam only. No HTTP initialization, rollover or reset API. */
export function createSharedBudgetState(config: Omit<SharedBudgetState, "contract" | "settled" | "reservations" | "receipts">): SharedBudgetState {
  const state: SharedBudgetState = structuredClone({ ...config, contract: "flow-shared-budget-v1", settled: zero(), reservations: {}, receipts: {} });
  validateState(state); return state;
}
function evidenceReady(s: SharedBudgetState, now: Date) {
  // Period change is NOT a reset. Reconciliation/provisioning is a separate future authorized action.
  if (s.epochDay !== now.toISOString().slice(0, 10) || s.epochMonth !== now.toISOString().slice(0, 7)) return false;
  return DIMENSIONS.every(d => {
    const m = s.evidence[d]; const age = now.getTime() - Date.parse(m.measuredAt ?? "");
    return m.basis === "measured" && m.scope === "account" && s.caps[d] !== null
      && Number.isFinite(age) && age >= 0 && age <= s.measurementTtlSeconds * 1000;
  });
}
/** Pure server-authoritative transitions; numeric semantics match BudgetLedger. */
export function transitionSharedBudget(original: SharedBudgetState, task: string, input: Pick<Input, "operation" | "vector" | "outcome">, now: Date) {
  validateState(original);
  const s = structuredClone(original);
  for (const r of Object.values(s.reservations)) if (["RESERVED", "RUNNING"].includes(r.status) && Date.parse(r.expiresAt) <= now.getTime()) r.status = "UNKNOWN";
  const r = s.reservations[task]; const op = input.operation;
  const result = (decision: string, reservation = r) => ({ state: s, result: { decision, taskId: task,
    ...(reservation ? { status: reservation.status, costUpperBound: reservation.cost, consumed: reservation.consumed,
      authorizedUpperBound: reservation.authorized, expiresAt: reservation.expiresAt } : {}) } as Record<string, unknown> });
  if (op === "reserve") {
    const cost = numbers(input.vector);
    if (r) { if (DIMENSIONS.some(d => r.cost[d] !== cost[d])) fail("budget_reservation_conflict", 409); return result("idempotent"); }
    if (!evidenceReady(s, now)) return result("unknown_metrics");
    if (Object.values(s.reservations).some(active => active.status === "OVER_BUDGET")) return result("over_budget");
    if (Object.keys(s.reservations).length >= MAX_RESERVATIONS) fail("budget_reservation_capacity", 409);
    let held = zero();
    for (const active of Object.values(s.reservations)) if (["RESERVED", "RUNNING", "UNKNOWN", "OVER_BUDGET"].includes(active.status)) held = add(held, active.cost);
    const current = Object.fromEntries(DIMENSIONS.map(d => [d, s.evidence[d].value])) as Numbers;
    const projected = add(add(add(current, s.settled), held), cost);
    if (DIMENSIONS.some(d => projected[d] > s.caps[d]!)) return result("over_budget");
    const reservation = { cost, consumed: zero(), authorized: zero(), status: "RESERVED", expiresAt: new Date(now.getTime() + s.reservationTtlSeconds * 1000).toISOString() };
    s.reservations[task] = reservation;
    return result(DIMENSIONS.some(d => s.targets[d] !== undefined && projected[d] > s.targets[d]!) ? "warn" : "allow", reservation);
  }
  if (!r) fail("budget_reservation_not_found", 409);
  if (op === "mark_running") {
    if (!["RESERVED", "RUNNING"].includes(r.status)) fail("reservation_not_running", 409);
    r.status = "RUNNING"; return result("running");
  }
  if (op === "authorize_step") {
    const cost = numbers(input.vector);
    if (!["RESERVED", "RUNNING"].includes(r.status)) return result("not_authorized");
    if (!evidenceReady(s, now)) return result("unknown_metrics");
    const proposed = add(r.authorized, cost);
    if (exceeds(proposed, r.cost)) return result("step_over_budget");
    r.authorized = proposed; r.status = "RUNNING"; return result("authorized");
  }
  if (op === "debit") {
    const delta = numbers(input.vector, true);
    if (!["RESERVED", "RUNNING"].includes(r.status)) return result("not_debitable");
    r.consumed = add(r.consumed, delta); r.status = exceeds(r.consumed, r.cost) ? "OVER_BUDGET" : "RUNNING";
    return result(r.status === "OVER_BUDGET" ? "over_budget" : "debited");
  }
  if (op === "settle") {
    if (!["SUCCEEDED", "FAILED_KNOWN", "UNKNOWN", "TIMEOUT"].includes(input.outcome ?? "")) fail("invalid_budget_outcome");
    if (r.settled && r.status !== "UNKNOWN") return result(r.settled);
    if (["UNKNOWN", "TIMEOUT"].includes(input.outcome!)) { r.status = "UNKNOWN"; r.settled = "unknown_outcome"; return result("unknown_outcome"); }
    const actual = numbers(input.vector, true);
    if (exceeds(r.consumed, actual)) fail("settlement_below_observed_consumption", 409);
    if (exceeds(actual, r.cost)) { r.status = "OVER_BUDGET"; r.settled = "over_budget"; return result("over_budget"); }
    s.settled = add(s.settled, actual); r.status = input.outcome!; r.settled = "settled"; return result("settled");
  }
  fail("invalid_budget_operation");
}
function response(payload: Record<string, unknown>, status = 200) { return Response.json(payload, { status, headers: { "Cache-Control": "private, no-store" } }); }
function authorized(request: Request, key: string) {
  const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (!key || token.length !== key.length) return false;
  let diff = 0; for (let i = 0; i < key.length; i++) diff |= token.charCodeAt(i) ^ key.charCodeAt(i); return diff === 0;
}
function identity(input: Input) {
  if (typeof input.runKey !== "string" || !parsePipelineRunKey(input.runKey) || typeof input.leaseOwner !== "string"
      || !/^[A-Za-z0-9_.:/-]{1,128}$/.test(input.leaseOwner) || !integer(input.attemptCount) || !input.attemptCount || input.attemptCount > 999999999
      || typeof input.targetDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(input.targetDate)) fail("invalid_budget_identity");
}
async function digest(v: unknown) {
  const canonical = (x: unknown): string => x === null || typeof x !== "object" ? JSON.stringify(x)
    : Array.isArray(x) ? `[${x.map(canonical).join(",")}]` : `{${Object.keys(x as object).sort().map(k => `${JSON.stringify(k)}:${canonical((x as Record<string, unknown>)[k])}`).join(",")}}`;
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(v))))].map(x => x.toString(16).padStart(2, "0")).join("");
}
async function body(request: Request) {
  if (!request.body) fail("budget_body_missing");
  const reader = request.body.getReader(); const chunks: Uint8Array[] = []; let total = 0;
  try { for (let count = 0;; count++) { const { done, value } = await reader.read(); if (done) break;
    total += value.length; if (total > 16384 || count >= 64) fail("budget_request_too_large", 413); chunks.push(value); }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  const bytes = new Uint8Array(total); let offset = 0; for (const c of chunks) { bytes.set(c, offset); offset += c.length; }
  try { const parsed = JSON.parse(new TextDecoder().decode(bytes)); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) fail("invalid_budget_request"); return parsed as Input; }
  catch { fail("invalid_budget_request"); }
}
export async function sharedBudgetRequest(request: Request, runtime: { DB?: DB; INGEST_API_KEY?: string }, clock = () => new Date(), limits: Caps = policyCaps) {
  if (!authorized(request, runtime.INGEST_API_KEY ?? "")) return response({ error: "unauthorized" }, 401);
  if (!runtime.DB) return response({ error: "storage_unavailable" }, 503);
  if (!["GET", "POST"].includes(request.method)) return response({ error: "method_not_allowed" }, 405);
  let rowsRead: number | null = 0; let rowsWritten: number | null = 0;
  const count = (meta?: { rows_read?: number; rows_written?: number }) => {
    rowsRead = rowsRead !== null && integer(meta?.rows_read) ? rowsRead + meta!.rows_read! : null;
    rowsWritten = rowsWritten !== null && integer(meta?.rows_written) ? rowsWritten + meta!.rows_written! : null;
  };
  try {
    const query = Object.fromEntries(new URL(request.url).searchParams);
    const input = request.method === "POST" ? await body(request) : { ...query, attemptCount: Number(query.attemptCount) } as Input;
    identity(input);
    const now = clock(); const cutoff = new Date(now.getTime() + 30000).toISOString();
    const db = runtime.DB.withSession?.("first-primary") ?? runtime.DB;
    // Lease and account state read on the primary. CAS below fences the lease again atomically.
    const lease = await db.prepare("SELECT run_key FROM pipeline_window WHERE run_key=? AND lease_owner=? AND attempt_count=? AND target_date=? AND recovery_id IS NULL AND status IN ('DISPATCHED','RUNNING') AND julianday(lease_expires_at)>julianday(?) LIMIT 1").bind(input.runKey, input.leaseOwner, input.attemptCount, input.targetDate, cutoff).all();
    count(lease.meta); if (!lease.results.length) fail("budget_lease_conflict", 409);
    const stored = await db.prepare("SELECT revision,state_json FROM pipeline_budget_account WHERE scope=? LIMIT 1").bind(BUDGET_SCOPE).all<{ revision: number; state_json: string }>();
    count(stored.meta); const row = stored.results[0]; if (!row) fail("budget_account_uninitialized", 409);
    if (!integer(row.revision) || new TextEncoder().encode(row.state_json).length > MAX_STATE_BYTES) fail("budget_state_corrupt", 503);
    const state = JSON.parse(row.state_json) as SharedBudgetState; validateState(state);
    for (const d of DIMENSIONS) {
      const cap = limits[d];
      if (!(cap === null || integer(cap))) fail("invalid_budget_policy", 503);
      state.caps[d] = cap === null || state.caps[d] === null ? null : Math.min(cap, state.caps[d]!);
    }
    if (request.method === "GET") return response({ revision: row.revision, contract: state.contract, epochDay: state.epochDay,
      evidenceReady: evidenceReady(state, now), rowsRead, rowsWritten });
    if (!/^[a-f0-9]{64}$/.test(input.operationId) || !integer(input.expectedRevision)) fail("invalid_budget_request");
    const semantic = { ...input } as Partial<Input>;
    delete semantic.expectedRevision;
    const hash = await digest(semantic); const prior = state.receipts[input.operationId];
    if (prior) {
      if (prior.hash !== hash) fail("budget_operation_conflict", 409);
      // Replayed allowance is the SAME logical action, not permission for a second I/O.
      return response({ ...prior.result, ...(prior.result.decision === "authorized" ? { decision: "not_authorized", priorDecision: "authorized" } : {}),
        revision: row.revision, replayed: true, rowsRead, rowsWritten });
    }
    if (row.revision !== input.expectedRevision) fail("budget_revision_conflict", 409);
    if (Object.keys(state.receipts).length >= MAX_RECEIPTS) fail("budget_receipt_capacity", 409);
    const task = `${input.runKey}/${input.leaseOwner}/${input.attemptCount}`;
    const next = transitionSharedBudget(state, task, input, now);
    next.state.receipts[input.operationId] = { hash, result: next.result };
    if (size(next.state) > MAX_STATE_BYTES) fail("budget_state_capacity", 409);
    const committed = await db.prepare("UPDATE pipeline_budget_account SET state_json=?,revision=revision+1,updated_at=? WHERE scope=? AND revision=? AND EXISTS (SELECT 1 FROM pipeline_window WHERE run_key=? AND lease_owner=? AND attempt_count=? AND target_date=? AND recovery_id IS NULL AND status IN ('DISPATCHED','RUNNING') AND julianday(lease_expires_at)>julianday(?)) RETURNING revision")
      .bind(JSON.stringify(next.state), now.toISOString(), BUDGET_SCOPE, row.revision, input.runKey, input.leaseOwner, input.attemptCount, input.targetDate, new Date(clock().getTime() + 30000).toISOString()).all<{ revision: number }>();
    count(committed.meta); if (!committed.results.length) fail("budget_revision_or_lease_conflict", 409);
    return response({ ...next.result, revision: committed.results[0].revision, replayed: false, rowsRead, rowsWritten });
  } catch (error) {
    return response({ error: error instanceof PipelineWindowError ? error.message : "budget_storage_unavailable", rowsRead, rowsWritten }, error instanceof PipelineWindowError ? error.status : 503);
  }
}
