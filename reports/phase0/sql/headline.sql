SELECT
  counts.pass_count AS passed_checks,
  counts.fail_count AS failed_checks,
  counts.warn_count AS warning_checks,
  (
    SELECT COUNT(*)
    FROM dataset_profiles
    WHERE dataset IN ('sse_etf', 'sse_margin', 'szse_margin', 'stock_connect_summary')
      AND coverage = '完整'
  ) AS complete_three_day_chains
FROM quality_counts AS counts;

