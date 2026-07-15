SELECT
  dataset,
  severity,
  message,
  remediation
FROM quality_findings
WHERE status <> 'pass'
ORDER BY severity, dataset, check_id;

