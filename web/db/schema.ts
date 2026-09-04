import { sql } from "drizzle-orm";
import { index, integer, primaryKey, real, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

export const dataSources = sqliteTable("data_source", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  sourceGrade: text("source_grade").notNull(),
  officialUrl: text("official_url").notNull(),
  enabled: integer("enabled", { mode: "boolean" }).notNull().default(true),
  createdAt: text("created_at").notNull(),
});

export const ingestRuns = sqliteTable("ingest_run", {
  id: text("id").primaryKey(),
  startedAt: text("started_at").notNull(),
  finishedAt: text("finished_at"),
  status: text("status").notNull(),
  errorCount: integer("error_count").notNull().default(0),
  manifestKey: text("manifest_key"),
}, (table) => [index("ingest_run_started_idx").on(table.startedAt)]);

export const dataReleases = sqliteTable("data_release", {
  id: text("id").primaryKey(),
  domain: text("domain").notNull(),
  tradeDate: text("trade_date").notNull(),
  status: text("status").notNull(),
  sourceId: text("source_id").notNull(),
  ingestRunId: text("ingest_run_id").notNull(),
  rowCount: integer("row_count").notNull(),
  activatedAt: text("activated_at"),
  createdAt: text("created_at").notNull(),
}, (table) => [
  uniqueIndex("data_release_domain_version_uq").on(table.domain, table.id),
  index("data_release_active_idx").on(table.domain, table.status, table.tradeDate),
]);

export const dashboardSnapshots = sqliteTable("dashboard_snapshot", {
  id: text("id").primaryKey(),
  asOf: text("as_of").notNull(),
  observedAt: text("observed_at").notNull(),
  payloadJson: text("payload_json").notNull(),
  publishedAt: text("published_at").notNull(),
  qualityPass: integer("quality_pass").notNull(),
  qualityWarn: integer("quality_warn").notNull(),
  qualityFail: integer("quality_fail").notNull(),
  r2ObjectKey: text("r2_object_key").notNull(),
}, (table) => [
  index("dashboard_snapshot_latest_idx").on(table.publishedAt),
  index("dashboard_snapshot_as_of_idx").on(table.asOf, table.publishedAt),
]);

export const historicalSnapshots = sqliteTable("historical_snapshot", {
  asOf: text("as_of").primaryKey(),
  observedAt: text("observed_at").notNull(),
  snapshotSha256: text("snapshot_sha256").notNull(),
  r2ObjectKey: text("r2_object_key").notNull(),
  manifestKey: text("manifest_key").notNull(),
  sourceCount: integer("source_count").notNull(),
  qualityPass: integer("quality_pass").notNull(),
  qualityWarn: integer("quality_warn").notNull(),
  qualityFail: integer("quality_fail").notNull(),
  createdAt: text("created_at").notNull(),
}, (table) => [
  index("historical_snapshot_created_idx").on(table.createdAt),
]);

export const releaseManifests = sqliteTable("release_manifest", {
  releaseId: text("release_id").primaryKey(),
  asOf: text("as_of").notNull(),
  schemaVersion: integer("schema_version").notNull(),
  manifestSha256: text("manifest_sha256").notNull(),
  manifestJson: text("manifest_json").notNull(),
  // Nullable during the rolling migration: v2 manifests did not carry an
  // observation timestamp, while v3 uses it to order same-day corrections.
  observedAt: text("observed_at"),
  status: text("status").notNull().default("STAGED"),
  createdAt: text("created_at").notNull(),
}, (table) => [
  index("release_manifest_as_of_idx").on(table.asOf, table.createdAt),
]);

export const activeReleases = sqliteTable("active_release", {
  slot: text("slot").primaryKey(),
  releaseId: text("release_id").notNull(),
  asOf: text("as_of").notNull(),
  manifestSha256: text("manifest_sha256").notNull(),
  observedAt: text("observed_at"),
  activatedAt: text("activated_at").notNull(),
});

export const marketHistoryDaily = sqliteTable("market_history_daily", {
  tradeDate: text("trade_date").primaryKey(),
  marginBalance: real("margin_balance"),
  marginChange: real("margin_change"),
  financingBuy: real("financing_buy"),
  lendingBalance: real("lending_balance"),
  lendingSellVolume: real("lending_sell_volume"),
  totalBalance: real("total_balance"),
  sseBalance: real("sse_balance"),
  szseBalance: real("szse_balance"),
  southboundNet: real("southbound_net"),
  southboundTurnover: real("southbound_turnover"),
  southboundSseNet: real("southbound_sse_net"),
  southboundSzseNet: real("southbound_szse_net"),
  northboundTurnover: real("northbound_turnover"),
  northboundEtfTurnover: real("northbound_etf_turnover"),
  northboundTradeCount: integer("northbound_trade_count"),
  marginAvailable: integer("margin_available", { mode: "boolean" }).notNull().default(false),
  connectAvailable: integer("connect_available", { mode: "boolean" }).notNull().default(false),
  sourceSnapshotSha256: text("source_snapshot_sha256").notNull(),
  observedAt: text("observed_at").notNull(),
  createdAt: text("created_at").notNull(),
  bseBalance: real("bse_balance"),
  marginScopeVersion: text("margin_scope_version").notNull().default("sse_szse_v1"),
}, (table) => [
  index("market_history_daily_date_idx").on(table.tradeDate),
]);

/**
 * Small control-plane ledger for source publication watermarks.  It is kept
 * separate from raw/read-model JSON so a later collection window can decide
 * whether it is waiting on an exchange or handling a real parser failure.
 */
export const sourceReadiness = sqliteTable("source_readiness", {
  targetDate: text("target_date").notNull(),
  sourceId: text("source_id").notNull(),
  status: text("status").notNull(),
  overallStatus: text("overall_status").notNull(),
  required: integer("required", { mode: "boolean" }).notNull().default(true),
  observedAt: text("observed_at").notNull(),
  nextCheckAt: text("next_check_at"),
  deadlineAt: text("deadline_at").notNull(),
  deadlineExceeded: integer("deadline_exceeded", { mode: "boolean" }).notNull().default(false),
  attempt: integer("attempt").notNull().default(1),
  observedDatesJson: text("observed_dates_json").notNull(),
  watermarkJson: text("watermark_json").notNull(),
  reason: text("reason").notNull(),
  runId: text("run_id").notNull(),
  evidenceJson: text("evidence_json").notNull(),
  updatedAt: text("updated_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.targetDate, table.sourceId] }),
  index("source_readiness_status_idx").on(table.status, table.nextCheckAt),
  index("source_readiness_target_idx").on(table.targetDate, table.updatedAt),
]);

/**
 * Small cross-scheduler lease ledger.  It is deliberately independent from
 * the heavy collection and release tables so a claim reads/writes one row
 * instead of scanning market history or ETF facts.
 */
export const pipelineWindow = sqliteTable("pipeline_window", {
  runKey: text("run_key").primaryKey(),
  pipeline: text("pipeline").notNull(),
  windowDate: text("window_date").notNull(),
  windowName: text("window_name").notNull(),
  contractVersion: text("contract_version").notNull(),
  targetDate: text("target_date"),
  status: text("status").notNull(),
  leaseOwner: text("lease_owner"),
  leaseExpiresAt: text("lease_expires_at"),
  sourceFingerprint: text("source_fingerprint"),
  githubRunId: text("github_run_id"),
  attemptCount: integer("attempt_count").notNull().default(0),
  lastErrorCode: text("last_error_code"),
  createdAt: text("created_at").notNull(),
  updatedAt: text("updated_at").notNull(),
  completedAt: text("completed_at"),
  recoveryId: text("recovery_id"),
}, (table) => [
  index("pipeline_window_status_lease_idx").on(table.status, table.leaseExpiresAt),
  index("pipeline_window_recovery_idx").on(table.recoveryId),
]);

/**
 * Audited authorization for repairing a pipeline window that was previously
 * marked complete or otherwise became inconsistent.  Registration is
 * intentionally separate from claiming the execution lease: this table holds
 * the immutable plan/scope and the exact pre-recovery row evidence, while the
 * pipeline_window row remains untouched until a guarded recovery claim wins.
 */
export const pipelineWindowRecovery = sqliteTable("pipeline_window_recovery", {
  recoveryId: text("recovery_id").primaryKey(),
  runKey: text("run_key").notNull(),
  planKey: text("plan_key").notNull(),
  planSha256: text("plan_sha256").notNull(),
  scopeSha256: text("scope_sha256").notNull(),
  expectedAttemptCount: integer("expected_attempt_count").notNull(),
  expectedUpdatedAt: text("expected_updated_at").notNull(),
  previousStateJson: text("previous_state_json").notNull(),
  reasonCode: text("reason_code").notNull(),
  approvalReference: text("approval_reference").notNull(),
  authorizedAt: text("authorized_at").notNull(),
  expiresAt: text("expires_at").notNull(),
  status: text("status").notNull(),
  latestReceiptKey: text("latest_receipt_key"),
  latestReceiptSha256: text("latest_receipt_sha256"),
  createdAt: text("created_at").notNull(),
  updatedAt: text("updated_at").notNull(),
  completedAt: text("completed_at"),
}, (table) => [
  uniqueIndex("pipeline_window_recovery_run_plan_uq").on(table.runKey, table.planSha256),
  uniqueIndex("pipeline_window_recovery_active_run_uq")
    .on(table.runKey)
    .where(sql`status IN ('ACTIVE', 'PAUSED')`),
  index("pipeline_window_recovery_status_expiry_idx").on(table.status, table.expiresAt),
  index("pipeline_window_recovery_run_status_idx").on(table.runKey, table.status),
]);

/**
 * PCF v2 control plane. These tables are intentionally additive and are not
 * read by the production release path until the later capability-cutover PR.
 * The collector uses them as a durable, resumable ledger rather than keeping
 * progress only inside a GitHub Actions process.
 */
export const pcfCaptureJob = sqliteTable("pcf_capture_job", {
  jobId: text("job_id").primaryKey(),
  targetDate: text("target_date").notNull(),
  previousDate: text("previous_date").notNull(),
  planSha256: text("plan_sha256").notNull(),
  calculationVersion: text("calculation_version").notNull(),
  status: text("status").notNull(),
  expectedCount: integer("expected_count").notNull(),
  requiredCount: integer("required_count").notNull(),
  unchangedCount: integer("unchanged_count").notNull(),
  newBaselineCount: integer("new_baseline_count").notNull(),
  missingCurrentCount: integer("missing_current_count").notNull(),
  capturedCount: integer("captured_count").notNull().default(0),
  validCount: integer("valid_count").notNull().default(0),
  failedCount: integer("failed_count").notNull().default(0),
  coveragePpm: integer("coverage_ppm").notNull().default(0),
  leaseOwner: text("lease_owner"),
  leaseExpiresAt: text("lease_expires_at"),
  nextAttemptAt: text("next_attempt_at"),
  canonicalManifestKey: text("canonical_manifest_key"),
  canonicalManifestSha256: text("canonical_manifest_sha256"),
  rawPackKey: text("raw_pack_key"),
  rawPackSha256: text("raw_pack_sha256"),
  errorJson: text("error_json").notNull().default("{}"),
  startedAt: text("started_at").notNull(),
  completedAt: text("completed_at"),
  updatedAt: text("updated_at").notNull(),
}, (table) => [
  uniqueIndex("pcf_capture_job_plan_sha256_uq").on(table.planSha256),
  index("pcf_capture_job_target_status_idx").on(table.targetDate, table.status),
  index("pcf_capture_job_status_attempt_idx").on(table.status, table.nextAttemptAt),
]);

export const pcfCaptureItem = sqliteTable("pcf_capture_item", {
  jobId: text("job_id").notNull(),
  etfCode: text("etf_code").notNull(),
  etfName: text("etf_name").notNull(),
  currentShares: integer("current_shares"),
  previousShares: integer("previous_shares"),
  shareDelta: integer("share_delta"),
  baselineStatus: text("baseline_status").notNull(),
  state: text("state").notNull(),
  attempts: integer("attempts").notNull().default(0),
  nextAttemptAt: text("next_attempt_at"),
  httpStatus: integer("http_status"),
  sourceTradingDay: text("source_trading_day"),
  sourcePreviousDay: text("source_previous_day"),
  rawObjectKey: text("raw_object_key"),
  rawObjectSha256: text("raw_object_sha256"),
  rawContentSha256: text("raw_content_sha256"),
  rawBytes: integer("raw_bytes"),
  componentCount: integer("component_count"),
  normalizedSha256: text("normalized_sha256"),
  failureReason: text("failure_reason"),
  observedAt: text("observed_at"),
  updatedAt: text("updated_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.jobId, table.etfCode] }),
  index("pcf_capture_item_state_attempt_idx").on(table.jobId, table.state, table.nextAttemptAt),
]);

export const historyArtifactPointer = sqliteTable("history_artifact_pointer", {
  sourceId: text("source_id").notNull(),
  dataset: text("dataset").notNull(),
  tradeDate: text("trade_date").notNull(),
  fileName: text("file_name").notNull(),
  revisionId: text("revision_id").notNull(),
  objectKey: text("object_key").notNull(),
  sha256: text("sha256").notNull(),
  contentEncoding: text("content_encoding"),
  rowCount: integer("row_count").notNull(),
  observedAt: text("observed_at").notNull(),
  status: text("status").notNull(),
  updatedAt: text("updated_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.sourceId, table.dataset, table.tradeDate, table.fileName] }),
  index("history_artifact_pointer_date_idx").on(table.dataset, table.tradeDate, table.status),
]);

export const etfDaily = sqliteTable("etf_daily", {
  releaseId: text("release_id").notNull(),
  tradeDate: text("trade_date").notNull(),
  market: text("market").notNull(),
  instrumentCode: text("instrument_code").notNull(),
  instrumentName: text("instrument_name").notNull(),
  category: text("category"),
  shares: integer("shares").notNull(),
  navMicros: integer("nav_micros"),
  navDate: text("nav_date"),
  dateBasis: text("date_basis").notNull(),
  sourceId: text("source_id").notNull(),
  observedAt: text("observed_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.releaseId, table.tradeDate, table.market, table.instrumentCode] }),
  index("etf_daily_lookup_idx").on(table.tradeDate, table.market, table.instrumentCode),
  index("etf_daily_category_idx").on(table.tradeDate, table.category),
]);

export const marginDaily = sqliteTable("margin_daily", {
  releaseId: text("release_id").notNull(),
  tradeDate: text("trade_date").notNull(),
  market: text("market").notNull(),
  financingBalanceYuan: integer("financing_balance_yuan").notNull(),
  financingBuyYuan: integer("financing_buy_yuan").notNull(),
  securitiesLendingBalanceYuan: integer("securities_lending_balance_yuan").notNull(),
  marginTotalBalanceYuan: integer("margin_total_balance_yuan").notNull(),
  sourceId: text("source_id").notNull(),
  observedAt: text("observed_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.releaseId, table.tradeDate, table.market] }),
  index("margin_daily_lookup_idx").on(table.tradeDate, table.market),
]);

export const connectDaily = sqliteTable("connect_daily", {
  releaseId: text("release_id").notNull(),
  tradeDate: text("trade_date").notNull(),
  market: text("market").notNull(),
  direction: text("direction").notNull(),
  currency: text("currency").notNull(),
  totalTurnover: integer("total_turnover").notNull(),
  buyTurnover: integer("buy_turnover"),
  sellTurnover: integer("sell_turnover"),
  netBuy: integer("net_buy"),
  etfTurnover: integer("etf_turnover").notNull(),
  sourceId: text("source_id").notNull(),
  observedAt: text("observed_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.releaseId, table.tradeDate, table.market, table.direction] }),
  index("connect_daily_lookup_idx").on(table.tradeDate, table.direction, table.market),
]);

export const connectTop = sqliteTable("connect_top", {
  releaseId: text("release_id").notNull(),
  tradeDate: text("trade_date").notNull(),
  market: text("market").notNull(),
  direction: text("direction").notNull(),
  rank: integer("rank").notNull(),
  instrumentCode: text("instrument_code").notNull(),
  instrumentName: text("instrument_name").notNull(),
  buyTurnover: integer("buy_turnover"),
  sellTurnover: integer("sell_turnover"),
  totalTurnover: integer("total_turnover").notNull(),
  sourceId: text("source_id").notNull(),
  observedAt: text("observed_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.releaseId, table.tradeDate, table.market, table.direction, table.rank] }),
  index("connect_top_lookup_idx").on(table.tradeDate, table.direction, table.market, table.rank),
]);
