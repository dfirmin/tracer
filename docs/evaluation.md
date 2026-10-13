# Evaluation gates

The unit tests prove the plumbing: validation, retry, budget, resume, review, CSV shape. They prove nothing about whether a real model traces lineage correctly. Three gates stand between this repo and trusting its output.

## Gate 1 — live smoke test

Build the image, set a key or gateway URL, run the fixture:

```bash
export TARGET_WORKSPACE=$PWD LINEAGE_OUTPUT=$PWD/out   # this repo contains examples/etl
docker compose run --rm tracer mart.daily_sales
```

Check: every stage produced structured output; `logs/*.problems` shows how often the entailment check fired and what the model did on retry; `net_usd` has exactly three rows (amount, discount, rate) with the filter stated in the Mapping Rule and no join-key rows; `dependencies.csv` has the two joins and two filters; `report.json` says `complete: true`; spend in `budget.json` is what you expect.

## Gate 2 — a labeled set

Pick 5–10 real targets across the shapes you actually have: Teradata volatile-table scripts, Databricks SQL notebooks, PySpark DataFrame chains, `spark.sql` f-strings, config-driven names, reused temp names, MERGE with prior-state reads. Have an engineer write the expected `lineage.csv` for each, tied to a commit. Keep the gold files outside the repo the agents read.

Measure per target: column enumeration recall; terminal source precision and recall; mapping-rule correctness (would running it reproduce the column?); business-rule faithfulness; citation entailment rate (already enforced, so measure how many retries it cost); boundary correctness (was anything called `repo_boundary` that the repo actually writes?); honest abstention (dead ends recorded rather than jumped). Track cost and wall time.

Inspect confident-and-wrong separately from disclosed gaps. A `high` confidence row that is wrong is the failure that matters; a `low` row with a clear `unresolved` reason is the system working.

Compare `LINEAGE_REVIEW=none|low|all` on the same set. If `all` catches errors that `low` misses at acceptable cost, change the default. If it mostly re-confirms, keep `low`.

## Gate 3 — scale

One 200+ column target under a spend ceiling. Interrupt it; confirm `--run <name>` resumes without re-running finished columns. Bump the commit; confirm it refuses. Watch for two columns disagreeing about a shared upstream transformation — the pipeline does not reconcile those today, and a shared reviewed-fact cache keyed by commit + scope is the next design step if they are common.
