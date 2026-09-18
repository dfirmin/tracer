# Lineage harness

This workspace is a lineage extraction run. The target repository is at `repo/` (read-only). Run artifacts live under `.lineage/<run>/`. Skills live in `.claude/skills/`; the vocabulary below is used throughout them.

**Read and reason, never parse.** The repo has no single dialect: Teradata SQL, Databricks SQL, SQL and Python notebooks, `spark.sql("...")` strings, Jinja/f-string templating, YAML/JSON config that supplies names. Read code the way a senior engineer reads it. Reach for `rg` to find, `Read` to understand. A regex is a search tool, never an answer.

**Every claim is cited.** Nothing goes in a manifest or finding without a `repo/...` path and line range you actually read.

**Gaps are a product.** A dead end recorded with its reason is worth more than a guess. Confidence and `unresolved_reason` exist so the CSV can be trusted where it is confident.

## Vocabulary

- **target** — the table/view the run is about.
- **build site** — code that writes the target (INSERT/MERGE/CREATE/saveAsTable/write…).
- **hop** — one step upstream: from an expression to the object and column it reads.
- **terminal source** — an object the repo reads but never creates: a physical table, an external schema, a file, an API. Tracing stops there.
- **object index** — the conductor's list of intermediate objects and where each is defined.
- **dead end** — a hop you cannot take from the repo alone (dynamic name, missing file, opaque UDF). Record it; don't jump it.
