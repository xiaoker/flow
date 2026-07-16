CREATE TABLE `dashboard_snapshot` (
	`id` text PRIMARY KEY NOT NULL,
	`as_of` text NOT NULL,
	`observed_at` text NOT NULL,
	`payload_json` text NOT NULL,
	`published_at` text NOT NULL,
	`quality_pass` integer NOT NULL,
	`quality_warn` integer NOT NULL,
	`quality_fail` integer NOT NULL,
	`r2_object_key` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `dashboard_snapshot_latest_idx` ON `dashboard_snapshot` (`published_at`);--> statement-breakpoint
CREATE INDEX `dashboard_snapshot_as_of_idx` ON `dashboard_snapshot` (`as_of`,`published_at`);