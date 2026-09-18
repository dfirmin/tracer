---
name: lineage-contract
description: The output contract for a lineage run — manifest and finding JSON shapes, the CSV columns they feed, and the meaning of each field. Use when writing a manifest or finding, or when unsure what a field expects.
---

Two JSON documents carry a run; a script joins them into the CSV. The schemas are the source of truth — read them from `/opt/lineage/schemas/` (`manifest.schema.json`, `finding.schema.json`) when a field's shape is in doubt; this file explains meaning, not shape.

## The CSV a row becomes

| Column | From | Meaning |
|---|---|---|
| Target Schema / Target Table | manifest `target` | as written in the build site, not as guessed from the prompt |
| Target Column | manifest `columns[].name` | the target's own spelling and case |
| Source Schema / Source Table / Source Column | finding `sources[]` | the **terminal** source. Empty column for literals |
| Source Type | `source_type` | `physical` warehouse table, `external` non-warehouse (file, API), `literal`, `derived`, `unknown` (dead end). `temp`/`volatile`/`cte`/`view`/`dataframe` appear only when a dead end stopped you at one |
| Mapping Rule | `mapping_rule` | the composed derivation, source → target, as expression text |
| Mapping Business Rule | `mapping_business_rule` | one plain-English sentence |
| Code URL | `code_path` + `code_lines` | resolved by the harness to `<base>/<path>#L<start>-L<end>` at the commit read |

One finding yields one row per source entry. A column with no resolvable source still yields one row (`Source Type: unknown`) so coverage is visible.

## Paths and lines

Paths are repo-relative and start with `repo/` (the harness strips it). Lines are 1-based and cover the statement that carries the claim — the whole expression, not the file. A citation to a file with no lines is a citation you did not read.

## Field hygiene

- Names keep the case and quoting the repo uses; the CSV is a lookup aid, not a normalisation.
- `mapping_rule` is expression text, never prose; `mapping_business_rule` is prose, never expression text.
- `unresolved_reason` says what you looked for, where, and what stopped you — enough for a human to finish the hop.
- `notes` in the manifest is for tracers: gotchas, ordering, dead ends already hit. Not a summary of the repo.
