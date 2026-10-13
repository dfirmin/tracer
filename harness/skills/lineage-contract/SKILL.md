---
name: lineage-contract
description: The output contract for a lineage run — manifest, finding and review JSON shapes, the CSV columns they feed, and the meaning of each field. Use when writing a manifest, finding or review, or when unsure what a field expects.
---

Three JSON documents carry a run; a script joins them into the CSVs. The schemas are the source of truth — read them from `/opt/lineage/schemas/` (`manifest.schema.json`, `finding.schema.json`, `review.schema.json`) when a field's shape is in doubt; this file explains meaning, not shape.

## The CSV a mapping becomes

| Column | From | Meaning |
|---|---|---|
| Target Schema / Target Table | manifest `target` | as written in the build site, not as guessed from the prompt |
| Target Column | manifest `columns[].name` | the target's own spelling and case |
| Source Schema / Source Table / Source Column | the mapping's terminal node | the **terminal value source**. Column empty for literal/runtime |
| Source Type | node `source_type` | `physical`, `external`, `view`, `file`, `literal`, `runtime`, `unresolved`. `temp`/`volatile`/`cte`/`dataframe` appear only when a dead end stopped you at one |
| Mapping Rule | `mapping_rule` | the composed derivation, source → target, as expression text; row-selection context inline |
| Mapping Business Rule | `mapping_business_rule` | one plain-English sentence |
| Code URL | mapping `evidence` | resolved by the harness to `<base>/<path>#L<start>-L<end>` at the commit read; several citations become ` \| `-separated links |

One mapping yields one row. A column with no mappings still yields one row (`Source Type: unresolved`) so coverage is visible.

Dependency edges (join, filter, group, window, order, control) never become rows here. They go to `dependencies.csv`, deduplicated per target table: one row per (kind, source column, predicate).

## Evidence

`{path, start_line, end_line, quote}`. Path is repo-relative and starts with `repo/`. Lines are 1-based and inclusive and cover the statement that carries the claim — the whole expression, not the file. `quote` is text that occurs within those lines, exactly as written; keep it to a distinctive fragment. The harness re-opens the file: a quote that is not there rejects the whole answer.

## Field hygiene

- Names keep the case and quoting the repo uses; the CSV is a lookup aid, not a normalisation.
- `mapping_rule` is expression text, never prose; `mapping_business_rule` is prose, never expression text.
- `scope` on a node is free text but specific: file, statement or cell, and write number when the name is reused.
- `unresolved` lines say what you looked for, where, and what stopped you — enough for a human to finish the hop.
- `notes` in the manifest is for tracers: gotchas, ordering, dead ends already hit. Not a summary of the repo.
- A review's `issues` are specific and cited: path, what is missing or contradicted, what you saw instead.
