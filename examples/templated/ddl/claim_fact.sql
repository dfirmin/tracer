-- Databricks SQL. Run once per environment with edw_schema set.
CREATE TABLE IF NOT EXISTS ${edw_schema}.claim_fact (
  claim_id          BIGINT,
  policy_number     STRING,
  line_of_business  STRING,
  paid_amount       DECIMAL(18,2),
  load_dt           DATE
);
