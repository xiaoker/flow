CREATE TABLE IF NOT EXISTS `release_manifest` (
  `release_id` text PRIMARY KEY NOT NULL,
  `as_of` text NOT NULL,
  `schema_version` integer NOT NULL,
  `manifest_sha256` text NOT NULL,
  `manifest_json` text NOT NULL,
  `status` text NOT NULL DEFAULT 'STAGED',
  `created_at` text NOT NULL
);

CREATE INDEX IF NOT EXISTS `release_manifest_as_of_idx`
  ON `release_manifest` (`as_of`, `created_at`);

CREATE TABLE IF NOT EXISTS `active_release` (
  `slot` text PRIMARY KEY NOT NULL,
  `release_id` text NOT NULL,
  `as_of` text NOT NULL,
  `manifest_sha256` text NOT NULL,
  `activated_at` text NOT NULL
);
