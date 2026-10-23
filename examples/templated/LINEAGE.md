jobs/schedule.txt is the run order; jobs/*.json are Databricks jobs whose tasks name a notebook and its parameters.
Schema names come from conf/env/<env>.yaml for notebooks and from scripts/run_stage.sh for BTEQ.
notebooks/generic_backfill.py is shared by several backfill jobs; its target is only in the job JSON.
