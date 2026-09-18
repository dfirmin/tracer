-- Job order: 01_stage.sql then 02_target.sql in one session.
CREATE VOLATILE TABLE vt_sales AS (
  SELECT order_id,
         amount - COALESCE(discount, 0) AS net_amount,
         customer_id
  FROM raw.orders
  WHERE status = 'PAID'
) WITH DATA ON COMMIT PRESERVE ROWS;
