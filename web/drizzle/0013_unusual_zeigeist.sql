CREATE TABLE `source_observation` (
	`observation_id` text PRIMARY KEY NOT NULL,
	`source_id` text NOT NULL,
	`dataset` text NOT NULL,
	`target_date` text NOT NULL,
	`source_business_date` text,
	`published_date` text,
	`date_basis` text NOT NULL,
	`status` text NOT NULL,
	`official_url` text NOT NULL,
	`request_params_json` text NOT NULL,
	`http_status` integer,
	`response_content_type` text,
	`raw_object_key` text,
	`raw_bytes` integer,
	`raw_sha256` text,
	`normalized_sha256` text,
	`source_fingerprint` text NOT NULL,
	`contract_version` text NOT NULL,
	`run_key` text,
	`observed_at` text NOT NULL,
	`reason_code` text,
	`evidence_json` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `source_observation_fingerprint_uq` ON `source_observation` (`source_fingerprint`);--> statement-breakpoint
CREATE INDEX `source_observation_target_source_idx` ON `source_observation` (`target_date`,`source_id`,`observed_at`);