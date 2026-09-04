CREATE UNIQUE INDEX `pipeline_window_recovery_active_run_uq` ON `pipeline_window_recovery` (`run_key`) WHERE status IN ('ACTIVE', 'PAUSED');
