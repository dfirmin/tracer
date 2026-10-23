# Templated fixture — `EDW.claim_fact`

A deliberately awkward mini ETL repo for tracer. No table name that matters is a literal at its write site.

| Writer | Shape | How the target name is formed |
|---|---|---|
| `bteq/stage_claim_lines.bteq` via `scripts/run_stage.sh` | Teradata BTEQ | `${STG_DB}` exported by the shell script per env |
| `notebooks/load_claim_fact.py` | PySpark `spark.sql` f-string, positional `INSERT OVERWRITE` | `cfg['edw_schema']` from `conf/env/<env>.yaml` + widget `target_table` from `jobs/claim_fact_nightly.json` |
| `notebooks/apply_adjustments.sql` | Databricks SQL `MERGE`, updates only `paid_amount` | widgets `edw_schema` / `raw_schema` from the job |
| `notebooks/generic_backfill.py` | generic loader | target exists **only** in `jobs/backfill_claim_fact.json` |

Column order comes from `ddl/claim_fact.sql`. Expected lineage for the prod context lives **outside** this directory, in `../templated_gold/`, so an agent reading this repo cannot see it.
