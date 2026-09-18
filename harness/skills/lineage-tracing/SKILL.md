---
name: lineage-tracing
description: Hop-by-hop discipline for tracing one column from a target expression back to its terminal sources across temp tables, CTEs, views, DataFrames and templated names. Use when tracing a column, composing a mapping rule, or grading confidence.
---

A trace is a chain of hops. Each hop reads one expression and names what it reads. Record hops as you go; the chain is the evidence.

## Taking a hop

At the current expression:

1. Resolve aliases to objects from the FROM/JOIN clause or the DataFrame variable's assignment. An alias resolved from memory instead of the clause is a guess.
2. For each object the expression reads, find its definition: the manifest's object index first, then search the repo for its writer (same searches as `etl-discovery`). CTEs are defined above in the same statement; DataFrames by the assignment that created the variable, walked backwards through every reassignment.
3. In that definition, find the expression that produces the column you followed. If the object exposes it as `SELECT *`, expand from the next object up.
4. Record the hop: object, column, expression, path, lines. Continue from the new expression.

## Where a branch ends

- **Terminal source** — the object is read but never written anywhere in the repo, or lives in a schema the repo only reads from. `source_type` is `physical` (a warehouse table) or `external` (a file, API, other system).
- **Literal** — a constant, `CURRENT_DATE`, a sequence, a hash of nothing upstream. `source_type: literal`, source column empty, the literal in `mapping_rule`.
- **Dead end** — a name computed at runtime you cannot resolve, a file the repo references but does not contain, a UDF/stored procedure whose body is not in the repo, a notebook `%run` you cannot find. Stop, record the object you reached as `source_type: unknown` with what you saw, and put the reason in `unresolved_reason`. A dead end is a finding; a jumped dead end is a fabrication.

## Fan-in

One target column often has several sources. Emit one source entry per contributing column:

- `CASE`, `COALESCE`, `NVL`, `IFNULL`, arithmetic, concatenation → one entry per source column, each with the full expression as its `mapping_rule` and the business rule phrased for that source's role.
- `UNION`/`UNION ALL` → one entry per branch.
- Joins: the joined table is a source only if the column reads from it. Join keys and filters are context — mention them in `mapping_rule` when they change the value (e.g. a filter that picks the latest row), otherwise leave them out.
- Window functions → the partition/order columns go in `mapping_rule`; the source is the column the function reads.

Temp, volatile, CTE, view, and DataFrame objects are hops, never terminal sources. The CSV reader wants to know which warehouse table the value ultimately came from; the hop chain in `hops` is where the intermediates live.

## Composing the mapping rule

`mapping_rule` is the derivation from terminal source to target column with every hop's transformation applied, written as the expression a reader could run. Substitute inward: if hop 1 is `TRIM(a.first_nm)` and hop 2 defines `a.first_nm` as `UPPER(src.fname)`, the rule is `TRIM(UPPER(src.fname))`. A straight pass-through is written as the source column name.

`mapping_business_rule` is one sentence in plain English, the kind a business analyst reads without SQL: *"First name from the policy system, uppercased and with surrounding spaces removed."* Name the source system or table in words, name the condition in words. No SQL keywords.

## Confidence

- **high** — every branch reached a terminal source or literal; every hop was read, not inferred; no placeholder left unresolved on the path.
- **medium** — a hop was inferred from a strong signal you could not fully read (e.g. a `SELECT *` expanded from DDL rather than the writer), or a placeholder resolved from a config with several environment values.
- **low** — any branch ended at a dead end, or the chain includes an object you never found a definition for.

## Completion

Done when every branch has an ending of one of the three kinds above, every hop is recorded with a read location, and each source entry carries a composed `mapping_rule`, a plain `mapping_business_rule`, and a citation. Count the branches you opened against the source entries you emitted before returning.
