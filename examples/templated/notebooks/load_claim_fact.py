# Databricks notebook source
# Nightly full load. Widgets come from the job's base_parameters.
import yaml

env = dbutils.widgets.get("env")
table = dbutils.widgets.get("target_table")
with open(f"/Workspace/Repos/etl/conf/env/{env}.yaml") as fh:
    cfg = yaml.safe_load(fh)
target = f"{cfg['edw_schema']}.{table}"

# COMMAND ----------

# Positional insert: column order is the DDL's (ddl/claim_fact.sql).
spark.sql(f"""
INSERT OVERWRITE {target}
SELECT l.claim_id,
       p.policy_number,
       UPPER(TRIM(p.lob_cd)) AS line_of_business,
       l.line_amount AS paid_amount,
       current_date() AS load_dt
FROM {cfg['stg_schema']}.claim_line_stg l
JOIN {cfg['raw_schema']}.policy p
  ON p.policy_id = l.policy_id
WHERE p.active_flag = 'Y'
""")
