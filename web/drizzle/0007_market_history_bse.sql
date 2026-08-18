ALTER TABLE `market_history_daily` ADD COLUMN `bse_balance` real;
ALTER TABLE `market_history_daily` ADD COLUMN `margin_scope_version` text NOT NULL DEFAULT 'sse_szse_v1';
