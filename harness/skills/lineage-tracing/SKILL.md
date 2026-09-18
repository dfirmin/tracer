---
name: lineage-tracing
description: Hop-by-hop discipline for tracing one column from a target expression back to its terminal sources across temp tables, CTEs, views, DataFrames and templated names, as a scoped graph with cited evidence. Use when tracing a column, deciding whether an input is a source or a dependency, composing a mapping rule, or grading confidence.
---

A trace is a graph: nodes are columns-at-a-definition, edges are hops. Record nodes and edges as you go; the graph is the evidence, and the harness checks it.

## Taking a hop

At the current expression:

1. Resolve aliases to objects from the FROM/JOIN clause or the DataFrame variable's assignment. An alias resolved from memory instead of the clause is a guess.
2. For each object the expression reads, find its definition: the manifest's object index first, then search the repo for its writer (same searches as `etl-discovery`). CTEs are defined above in the same statement; DataFrames by the assignment that created the variable, walked backwards through every reassignment.
3. In that definition, find the expression that produces the column you followed. If the object exposes it as `SELECT *`, expand from the next object up.
4. Add a node for the column you arrived at and an edge from it to the node you came from. Cite the lines you read on both. Continue from the new expression.

## Value or dependency

Every edge carries a `dependency_kind`. Ask: *does the source's value flow into the target, or does it decide which rows arrive?*

- **value** — the column appears in the target's expression: operands of arithmetic, CASE results, COALESCE arguments, concatenation parts, the column a window function reads, UNION branch columns.
- **join** — join keys on either side. The joined table becomes a value source only through columns that appear in the expression.
- **filter** — WHERE/HAVING/QUALIFY predicates, MERGE match conditions, `df.filter(...)`.
- **group** — GROUP BY / DISTINCT keys.
- **window** — PARTITION BY / ORDER BY / frame of a window function.
- **order** — ORDER BY that changes which row wins (with LIMIT, TOP, ROW_NUMBER = 1).
- **control** — a flag, config, or CASE condition that selects a branch without contributing a value.

Only value edges lead to mappings. A CASE's *result* branches are value; its *condition* is control, and the condition's columns are dependencies. Row-selection context still belongs in the `mapping_rule` text (`... where fx.is_current = 1`) — the CSV reader needs it — but the filter column is not a source.

## Scope

A node is a column at one state. When the same temp/volatile table, DataFrame variable or view is written more than once in the job, each write is a different node with a different `scope` (`repo/etl/load.sql:INSERT vt_sales (2nd write)`, `notebook.py:cell 7 df = df.withColumn(...)`). A self-read (`MERGE INTO t USING ... t`) is a previous-state dependency: make the previous state its own terminal node with a boundary reason rather than drawing a cycle.

## Where a branch ends

- **repo_boundary** — the object is read but never written anywhere in the repo, or lives in a schema the repo only reads from. `source_type` `physical` (warehouse table), `external` (another system), `view` (a view whose DDL is not in the repo), or `file`. Record `search_notes`: the names you searched and where, so a reviewer can see no producer exists.
- **literal** / **runtime** — a constant, or `CURRENT_DATE`, a sequence, a job parameter with a known value. `column` holds the expression text.
- **unresolved** — a name computed at runtime you cannot resolve, a file the repo references but does not contain, a UDF/stored procedure whose body is not in the repo, a notebook `%run` you cannot find. Stop, mark the node `unresolved` with what you saw, add a line to `unresolved`. A dead end is a finding; a jumped dead end is a fabrication.

Temp, volatile, CTE, view-in-repo and DataFrame nodes are hops, never terminals — unless a dead end stops you at one.

## Fan-in

One target column often has several value sources. Emit one mapping per terminal value source:

- `CASE`, `COALESCE`, `NVL`, `IFNULL`, arithmetic, concatenation → one mapping per source column, each with the full expression as its `mapping_rule` and the business rule phrased for that source's role.
- `UNION`/`UNION ALL` → one mapping per branch.
- Window functions → the source is the column the function reads; partition/order columns are `window` dependencies and appear in `mapping_rule` text.
- `COUNT(*)` → the row set is the source: a node with column `*` on the counted relation.

## Composing the mapping rule

`mapping_rule` is the derivation from terminal source to target column with every hop's transformation applied, written as the expression a reader could run. Substitute inward: if hop 1 is `TRIM(a.first_nm)` and hop 2 defines `a.first_nm` as `UPPER(src.fname)`, the rule is `TRIM(UPPER(src.fname))`. A straight pass-through is written as the source column name. When hops cross dialects (PySpark into Teradata SQL) and one expression would misstate the semantics, write a labeled ordered sequence instead of inventing SQL.

`mapping_business_rule` is one sentence in plain English, the kind a business analyst reads without SQL: *"First name from the policy system, uppercased and with surrounding spaces removed."* Name the source system or table in words, name the condition in words. No SQL keywords.

## Evidence

Every node, edge and mapping cites path + lines + a verbatim quote from those lines. The harness re-opens the file and rejects a quote that is not there, and rejects a mapping whose source is reachable only through dependency edges. Cite what you read, quote a short exact fragment, and cite the raw notebook file at its real line numbers.

## Confidence

- **high** — every branch reached a terminal source, literal or runtime; every hop was read, not inferred; no placeholder left unresolved on the path.
- **medium** — a hop was inferred from a strong signal you could not fully read (a `SELECT *` expanded from DDL rather than the writer), or a placeholder resolved from a config with several environment values.
- **low** — any branch ended at a dead end, or the chain includes an object you never found a definition for.

## Completion

Done when every branch has an ending of one of the kinds above, every node and edge is cited, every terminal value source has a mapping with a composed `mapping_rule` and a plain `mapping_business_rule`, and no dependency-only source is mapped. Count the value branches you opened against the mappings you emitted before returning.
