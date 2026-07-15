SELECT
  dataset,
  CASE WHEN status = 'pass' THEN '通过' ELSE '失败' END AS status,
  max_abs_delta_yuan,
  tolerance_yuan
FROM quality_findings
WHERE check_id = 'consistency.margin_total'
ORDER BY dataset;

