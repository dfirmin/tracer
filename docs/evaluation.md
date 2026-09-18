# Evaluation and next integration step

## Gate 1: live container smoke test

Build the pinned image, set an API key and run a small committed fixture. Verify the CLI accepts all flags, can actually invoke Read/Glob/Grep, returns `structured_output`, and produces successful independent reviews. Check that exit codes and budget reservations behave as documented. No live Claude or Docker execution was possible in the implementation environment, so this gate is still open.

## Gate 2: a labeled repository set

Create expert-reviewed expected lineage for representative targets, covering Teradata volatile tables, Databricks SQL, raw notebooks, Python dataframe chains, spark.sql string construction, templates/config/includes, reused temporary names, merges and incremental historical state. Include adversarial examples: same-named tables across environments, missing includes, SELECT *, opaque UDFs and contradictory writers.

Keep each gold result tied to a commit and execution context. Separate evaluation source from expected answers so agents cannot read the gold mappings. Run the actual pinned Claude harness, not a transport fake.

Measure column enumeration recall, leaf-source precision/recall, intermediate-hop correctness, row-selection dependency recall, citation entailment, boundary correctness, business-description faithfulness and honest abstention. Track wall time, token/cost totals and reviewer corrections. Inspect false completeness claims separately from partial results: a confident wrong mapping is more damaging than a disclosed gap.

Do not equate valid JSON, citation existence or reviewer acceptance with semantic accuracy. Reviewer and tracer can share model blind spots. Use human review of a representative sample and every high-risk/uncertain result. Require an independent expected column manifest when the target schema is available outside the repository.

## Gate 3: scale and operational adoption

Run a real 200+ column target under an explicit spend ceiling. Interrupt midway and confirm complete columns are reused while incomplete columns resume. Change a source commit/config/prompt and confirm stale results are rejected. Exercise timeouts, API errors and malformed outputs. Confirm target code is never executed and credentials are absent from artifacts.

Use actual repeated upstream reads and costs to decide whether a shared fact cache is justified. If so, cache reviewed subgraphs with immutable keys and provenance, and make consumers verify the referenced facts. If two columns disagree about a common transformation, require a focused adjudication session rather than selecting whichever completed first. This reference version does not automatically reconcile cross-column semantic disagreements.

## Inputs needed for the next iteration

- One real repository URL and access to its clean checkout.
- A target table/view and its production job/environment or config selection.
- An expected ordered column list when available.
- A few manually verified mappings, plus your acceptable accuracy and per-table budget.

Those inputs enable the first live integration and repository-specific evaluation without building a dialect parser.
