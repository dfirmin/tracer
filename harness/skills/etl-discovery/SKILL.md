---
name: etl-discovery
description: Find where a table or view is built in an ETL repo of mixed shapes (Teradata/Databricks SQL, SQL and Python notebooks, spark.sql strings, templated names, config-driven loaders). Use when locating build sites, enumerating a target's columns, or resolving a placeholder table name.
---

Discovery is search plus reading. Search finds candidates; reading decides. A candidate you have not read is not a build site.

## Finding the build site

Search for the target name in every form it could take, widest first:

- bare name (`claim_fact`), qualified (`edw.claim_fact`), quoted, upper/lower case, and the name split from its schema (`"claim_fact"` next to a `schema=` or `database=` variable);
- writers, near the name: `INSERT`, `MERGE`, `UPDATE`, `CREATE [OR REPLACE] [VOLATILE|TEMP|TEMPORARY] TABLE|VIEW`, `REPLACE VIEW`, `saveAsTable`, `insertInto`, `.write.`, `write.format`, `COPY INTO`, `dbt` `ref(`, `MLOAD`/`FASTLOAD`/`TPT`;
- shapes that hide names: f-strings and `.format(` with the name as a fragment, Jinja `{{ }}`, YAML/JSON keys (`target_table:`, `dest:`), Airflow/Databricks-job definitions that pass names as params.

When several files write the target, order them by execution: the orchestration (DAG, job JSON, shell driver, BTEQ script order) says which runs last, and the last writer's SELECT list is the column authority.

Views over tables count as build sites when the target is the view; the table underneath is then the first hop, not the answer.

## Enumerating columns

The authoritative list is the final writer's projection: the INSERT column list if present, else the SELECT list in order, else the DataFrame's schema at the write. A DDL file gives names and order too; if DDL and projection disagree, the projection wins and the disagreement goes in `notes`.

`SELECT *` and `df.select("*")` are unresolved until expanded from the upstream object's own definition. Positional inserts (no column list) map by position to the DDL — say so in `notes`.

## Resolving placeholders

A placeholder resolves from, in order: a variable set in the same file; a config file the file reads (look for `load(`, `open(`, `yaml`, `json`, `configparser`, `dbutils.widgets`, `getArgument`, `os.environ`); a job/DAG definition that passes it in. Record the resolved value with its location. Environment-specific values (dev/prod) are all recorded; the run does not pick one.

## Completion

You are done when: every writer of the target has been read and listed; the column list is exhaustive, ordered, and every column has a read location; every object the writer reads is in the object index with a definition location or an `external`/`physical` mark; every placeholder is resolved or recorded as unresolved. Anything short of that is *fog* — name it in `notes` rather than fill it in.
