CREATE TABLE `connect_daily` (
	`release_id` text NOT NULL,
	`trade_date` text NOT NULL,
	`market` text NOT NULL,
	`direction` text NOT NULL,
	`currency` text NOT NULL,
	`total_turnover` integer NOT NULL,
	`buy_turnover` integer,
	`sell_turnover` integer,
	`net_buy` integer,
	`etf_turnover` integer NOT NULL,
	`source_id` text NOT NULL,
	`observed_at` text NOT NULL,
	PRIMARY KEY(`release_id`, `trade_date`, `market`, `direction`)
);
--> statement-breakpoint
CREATE INDEX `connect_daily_lookup_idx` ON `connect_daily` (`trade_date`,`direction`,`market`);--> statement-breakpoint
CREATE TABLE `connect_top` (
	`release_id` text NOT NULL,
	`trade_date` text NOT NULL,
	`market` text NOT NULL,
	`direction` text NOT NULL,
	`rank` integer NOT NULL,
	`instrument_code` text NOT NULL,
	`instrument_name` text NOT NULL,
	`buy_turnover` integer,
	`sell_turnover` integer,
	`total_turnover` integer NOT NULL,
	`source_id` text NOT NULL,
	`observed_at` text NOT NULL,
	PRIMARY KEY(`release_id`, `trade_date`, `market`, `direction`, `rank`)
);
--> statement-breakpoint
CREATE INDEX `connect_top_lookup_idx` ON `connect_top` (`trade_date`,`direction`,`market`,`rank`);--> statement-breakpoint
CREATE TABLE `data_release` (
	`id` text PRIMARY KEY NOT NULL,
	`domain` text NOT NULL,
	`trade_date` text NOT NULL,
	`status` text NOT NULL,
	`source_id` text NOT NULL,
	`ingest_run_id` text NOT NULL,
	`row_count` integer NOT NULL,
	`activated_at` text,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `data_release_domain_version_uq` ON `data_release` (`domain`,`id`);--> statement-breakpoint
CREATE INDEX `data_release_active_idx` ON `data_release` (`domain`,`status`,`trade_date`);--> statement-breakpoint
CREATE TABLE `data_source` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`source_grade` text NOT NULL,
	`official_url` text NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `etf_daily` (
	`release_id` text NOT NULL,
	`trade_date` text NOT NULL,
	`market` text NOT NULL,
	`instrument_code` text NOT NULL,
	`instrument_name` text NOT NULL,
	`category` text,
	`shares` integer NOT NULL,
	`nav_micros` integer,
	`date_basis` text NOT NULL,
	`source_id` text NOT NULL,
	`observed_at` text NOT NULL,
	PRIMARY KEY(`release_id`, `trade_date`, `market`, `instrument_code`)
);
--> statement-breakpoint
CREATE INDEX `etf_daily_lookup_idx` ON `etf_daily` (`trade_date`,`market`,`instrument_code`);--> statement-breakpoint
CREATE INDEX `etf_daily_category_idx` ON `etf_daily` (`trade_date`,`category`);--> statement-breakpoint
CREATE TABLE `ingest_run` (
	`id` text PRIMARY KEY NOT NULL,
	`started_at` text NOT NULL,
	`finished_at` text,
	`status` text NOT NULL,
	`error_count` integer DEFAULT 0 NOT NULL,
	`manifest_key` text
);
--> statement-breakpoint
CREATE INDEX `ingest_run_started_idx` ON `ingest_run` (`started_at`);--> statement-breakpoint
CREATE TABLE `margin_daily` (
	`release_id` text NOT NULL,
	`trade_date` text NOT NULL,
	`market` text NOT NULL,
	`financing_balance_yuan` integer NOT NULL,
	`financing_buy_yuan` integer NOT NULL,
	`securities_lending_balance_yuan` integer NOT NULL,
	`margin_total_balance_yuan` integer NOT NULL,
	`source_id` text NOT NULL,
	`observed_at` text NOT NULL,
	PRIMARY KEY(`release_id`, `trade_date`, `market`)
);
--> statement-breakpoint
CREATE INDEX `margin_daily_lookup_idx` ON `margin_daily` (`trade_date`,`market`);