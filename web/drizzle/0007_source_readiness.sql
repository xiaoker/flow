CREATE TABLE `source_readiness` (
	`target_date` text NOT NULL,
	`source_id` text NOT NULL,
	`status` text NOT NULL,
	`overall_status` text NOT NULL,
	`required` integer DEFAULT true NOT NULL,
	`observed_at` text NOT NULL,
	`next_check_at` text,
	`deadline_at` text NOT NULL,
	`deadline_exceeded` integer DEFAULT false NOT NULL,
	`attempt` integer DEFAULT 1 NOT NULL,
	`observed_dates_json` text NOT NULL,
	`watermark_json` text NOT NULL,
	`reason` text NOT NULL,
	`run_id` text NOT NULL,
	`evidence_json` text NOT NULL,
	`updated_at` text NOT NULL,
	PRIMARY KEY(`target_date`, `source_id`)
);
--> statement-breakpoint
CREATE INDEX `source_readiness_status_idx` ON `source_readiness` (`status`,`next_check_at`);--> statement-breakpoint
CREATE INDEX `source_readiness_target_idx` ON `source_readiness` (`target_date`,`updated_at`);