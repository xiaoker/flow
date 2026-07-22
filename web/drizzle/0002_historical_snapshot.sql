CREATE TABLE `historical_snapshot` (
	`as_of` text PRIMARY KEY NOT NULL,
	`observed_at` text NOT NULL,
	`snapshot_sha256` text NOT NULL,
	`r2_object_key` text NOT NULL,
	`manifest_key` text NOT NULL,
	`source_count` integer NOT NULL,
	`quality_pass` integer NOT NULL,
	`quality_warn` integer NOT NULL,
	`quality_fail` integer NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `historical_snapshot_created_idx` ON `historical_snapshot` (`created_at`);
