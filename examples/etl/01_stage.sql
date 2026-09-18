CREATE VOLATILE TABLE vt_sales AS (
  SELECT o.order_id,
         o.customer_id,
         o.amount - COALESCE(o.discount, 0) AS net_amount
  FROM raw.orders AS o
  WHERE o.status = 'PAID'
) WITH DATA ON COMMIT PRESERVE ROWS;
