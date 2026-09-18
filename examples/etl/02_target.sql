CREATE TABLE mart.daily_sales AS (
  SELECT s.order_id,
         s.net_amount * fx.rate AS net_usd,
         COALESCE(c.segment, 'UNKNOWN') AS segment
  FROM vt_sales AS s
  JOIN raw.customers AS c ON s.customer_id = c.customer_id
  JOIN raw.exchange_rates AS fx ON c.currency = fx.currency
  WHERE fx.is_current = 1
) WITH DATA;
