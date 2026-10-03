CREATE TABLE `pipeline_budget_account` (
	`scope` text PRIMARY KEY NOT NULL,
	`revision` integer DEFAULT 0 NOT NULL,
	`state_json` text NOT NULL,
	`updated_at` text NOT NULL
);
