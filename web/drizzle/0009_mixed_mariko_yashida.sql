CREATE TABLE `history_artifact_pointer` (
	`source_id` text NOT NULL,
	`dataset` text NOT NULL,
	`trade_date` text NOT NULL,
	`file_name` text NOT NULL,
	`revision_id` text NOT NULL,
	`object_key` text NOT NULL,
	`sha256` text NOT NULL,
	`content_encoding` text,
	`row_count` integer NOT NULL,
	`observed_at` text NOT NULL,
	`status` text NOT NULL,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`source_id`, `dataset`, `trade_date`, `file_name`)
);
--> statement-breakpoint
CREATE INDEX `history_artifact_pointer_date_idx` ON `history_artifact_pointer` (`dataset`,`trade_date`,`status`);--> statement-breakpoint
CREATE TABLE `pcf_capture_item` (
	`job_id` text NOT NULL,
	`etf_code` text NOT NULL,
	`etf_name` text NOT NULL,
	`current_shares` integer,
	`previous_shares` integer,
	`share_delta` integer,
	`baseline_status` text NOT NULL,
	`state` text NOT NULL,
	`attempts` integer DEFAULT 0 NOT NULL,
	`next_attempt_at` text,
	`http_status` integer,
	`source_trading_day` text,
	`source_previous_day` text,
	`raw_object_key` text,
	`raw_object_sha256` text,
	`raw_content_sha256` text,
	`raw_bytes` integer,
	`component_count` integer,
	`normalized_sha256` text,
	`failure_reason` text,
	`observed_at` text,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`job_id`, `etf_code`)
);
--> statement-breakpoint
CREATE INDEX `pcf_capture_item_state_attempt_idx` ON `pcf_capture_item` (`job_id`,`state`,`next_attempt_at`);--> statement-breakpoint
CREATE TABLE `pcf_capture_job` (
	`job_id` text PRIMARY KEY NOT NULL,
	`target_date` text NOT NULL,
	`previous_date` text NOT NULL,
	`plan_sha256` text NOT NULL,
	`calculation_version` text NOT NULL,
	`status` text NOT NULL,
	`expected_count` integer NOT NULL,
	`required_count` integer NOT NULL,
	`unchanged_count` integer NOT NULL,
	`new_baseline_count` integer NOT NULL,
	`missing_current_count` integer NOT NULL,
	`captured_count` integer DEFAULT 0 NOT NULL,
	`valid_count` integer DEFAULT 0 NOT NULL,
	`failed_count` integer DEFAULT 0 NOT NULL,
	`coverage_ppm` integer DEFAULT 0 NOT NULL,
	`lease_owner` text,
	`lease_expires_at` text,
	`next_attempt_at` text,
	`canonical_manifest_key` text,
	`canonical_manifest_sha256` text,
	`raw_pack_key` text,
	`raw_pack_sha256` text,
	`error_json` text DEFAULT '{}' NOT NULL,
	`started_at` text NOT NULL,
	`completed_at` text,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `pcf_capture_job_plan_sha256_uq` ON `pcf_capture_job` (`plan_sha256`);--> statement-breakpoint
CREATE INDEX `pcf_capture_job_target_status_idx` ON `pcf_capture_job` (`target_date`,`status`);--> statement-breakpoint
CREATE INDEX `pcf_capture_job_status_attempt_idx` ON `pcf_capture_job` (`status`,`next_attempt_at`);