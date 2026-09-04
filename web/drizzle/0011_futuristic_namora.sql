CREATE TABLE `pipeline_window_recovery` (
	`recovery_id` text PRIMARY KEY NOT NULL,
	`run_key` text NOT NULL,
	`plan_key` text NOT NULL,
	`plan_sha256` text NOT NULL,
	`scope_sha256` text NOT NULL,
	`expected_attempt_count` integer NOT NULL,
	`expected_updated_at` text NOT NULL,
	`previous_state_json` text NOT NULL,
	`reason_code` text NOT NULL,
	`approval_reference` text NOT NULL,
	`authorized_at` text NOT NULL,
	`expires_at` text NOT NULL,
	`status` text NOT NULL,
	`latest_receipt_key` text,
	`latest_receipt_sha256` text,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	`completed_at` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `pipeline_window_recovery_run_plan_uq` ON `pipeline_window_recovery` (`run_key`,`plan_sha256`);--> statement-breakpoint
CREATE INDEX `pipeline_window_recovery_status_expiry_idx` ON `pipeline_window_recovery` (`status`,`expires_at`);--> statement-breakpoint
CREATE INDEX `pipeline_window_recovery_run_status_idx` ON `pipeline_window_recovery` (`run_key`,`status`);--> statement-breakpoint
ALTER TABLE `pipeline_window` ADD `recovery_id` text;--> statement-breakpoint
CREATE INDEX `pipeline_window_recovery_idx` ON `pipeline_window` (`recovery_id`);
