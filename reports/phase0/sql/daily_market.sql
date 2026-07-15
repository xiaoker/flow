WITH margin_daily AS (
  SELECT
    trade_date,
    SUM(financing_balance_yuan) / 1000000000.0 AS financing_balance_billion_yuan
  FROM normalized_margin
  GROUP BY trade_date
),
southbound_daily AS (
  SELECT
    trade_date,
    SUM(net_buy) / 1000000000.0 AS southbound_net_buy_billion_hkd
  FROM normalized_connect_summary
  WHERE direction = 'southbound'
  GROUP BY trade_date
),
northbound_daily AS (
  SELECT
    trade_date,
    SUM(total_turnover) / 1000000000.0 AS northbound_turnover_billion_rmb
  FROM normalized_connect_summary
  WHERE direction = 'northbound'
  GROUP BY trade_date
)
SELECT
  margin_daily.trade_date,
  ROUND(margin_daily.financing_balance_billion_yuan, 3) AS financing_balance_billion_yuan,
  ROUND(southbound_daily.southbound_net_buy_billion_hkd, 3) AS southbound_net_buy_billion_hkd,
  ROUND(northbound_daily.northbound_turnover_billion_rmb, 3) AS northbound_turnover_billion_rmb
FROM margin_daily
JOIN southbound_daily USING (trade_date)
JOIN northbound_daily USING (trade_date)
ORDER BY margin_daily.trade_date;

