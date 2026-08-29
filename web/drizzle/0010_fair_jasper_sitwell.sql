CREATE TABLE `pipeline_window` (
	`run_key` text PRIMARY KEY NOT NULL,
	`pipeline` text NOT NULL,
	`window_date` text NOT NULL,
	`window_name` text NOT NULL,
	`contract_version` text NOT NULL,
	`target_date` text,
	`status` text NOT NULL,
	`lease_owner` text,
	`lease_expires_at` text,
	`source_fingerprint` text,
	`github_run_id` text,
	`attempt_count` integer DEFAULT 0 NOT NULL,
	`last_error_code` text,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	`completed_at` text
);
--> statement-breakpoint
CREATE INDEX `pipeline_window_status_lease_idx` ON `pipeline_window` (`status`,`lease_expires_at`);--> statement-breakpoint
