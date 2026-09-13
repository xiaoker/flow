CREATE TABLE `market_day_state` (
	`target_date` text PRIMARY KEY NOT NULL,
	`state` text NOT NULL,
	`projection_version` text NOT NULL,
	`source_fingerprint` text NOT NULL,
	`probe_fingerprint` text NOT NULL,
	`required_source_count` integer NOT NULL,
	`verified_source_count` integer NOT NULL,
	`pending_source_count` integer NOT NULL,
	`unavailable_source_count` integer NOT NULL,
	`next_action` text NOT NULL,
	`probe_json` text NOT NULL,
	`sources_json` text NOT NULL,
	`projected_at` text NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `market_day_state_action_idx` ON `market_day_state` (`next_action`,`updated_at`);