CREATE TABLE `market_history_daily` (
	`trade_date` text PRIMARY KEY NOT NULL,
	`margin_balance` real,
	`margin_change` real,
	`financing_buy` real,
	`lending_balance` real,
	`lending_sell_volume` real,
	`total_balance` real,
	`sse_balance` real,
	`szse_balance` real,
	`southbound_net` real,
	`southbound_turnover` real,
	`southbound_sse_net` real,
	`southbound_szse_net` real,
	`northbound_turnover` real,
	`northbound_etf_turnover` real,
	`northbound_trade_count` integer,
	`margin_available` integer NOT NULL DEFAULT 0,
	`connect_available` integer NOT NULL DEFAULT 0,
	`source_snapshot_sha256` text NOT NULL,
	`observed_at` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `market_history_daily_date_idx` ON `market_history_daily` (`trade_date`);
