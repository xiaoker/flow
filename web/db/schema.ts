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
}, (table) => [
  index("market_history_daily_date_idx").on(table.tradeDate),
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
