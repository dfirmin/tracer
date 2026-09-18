# tracer

Produces a source-to-target mapping CSV for one table/view by **reading** the ETL repo that builds it. No SQL parser, no per-dialect adapters: a conductor discovers how the target is built, one tracer per column walks the derivation hop by hop to its terminal sources as a cited graph, the harness checks every citation against the file, an optional reviewer replays the trace, and a small script joins the findings into a CSV with a code link on every row.

Works against any workspace: the harness (`CLAUDE.md`, agents, skills) lives at the container's project root and the target repo is mounted beneath it at `repo/`, so nothing is written into the repo and no repo-specific setup is needed.

## Run

```bash
export TARGET_WORKSPACE=/path/to/etl-repo   # clean, committed checkout
export LINEAGE_OUTPUT=$PWD/out
export ANTHROPIC_API_KEY=...                 # or ANTHROPIC_BASE_URL for an internal gateway
docker compose build
docker compose run --rm tracer edw.claim_fact
```

Or without compose: `docker run --rm -e ANTHROPIC_API_KEY -v $TARGET_WORKSPACE:/work/repo:ro -v $LINEAGE_OUTPUT:/work/.lineage tracer edw.claim_fact [--repo git@... --ref main] [--run name]`.

Output in `out/<run>/`:

| File | Meaning |
|---|---|
| `lineage.csv` | the deliverable: one row per terminal **value** source of each target column. Named `lineage.partial.csv` when any column has a gap |
| `dependencies.csv` | join keys, filters, group/window/order columns and control flags, deduplicated per target table — the impact-analysis view |
| `gaps.md` | columns that did not fully resolve, with the tracer's reasons |
| `report.json` | per-column status, confidence, review outcome, spend |
| `manifest.json`, `findings/*.json`, `reviews/*.json` | the agents' validated outputs; `findings/` are full graphs with scope and evidence |
| `budget.json`, `logs/` | spend ledger; raw session envelopes, stderr, and `*.problems` (what the validator rejected) |

Exit codes: `0` complete, `3` partial (a column has a gap or the run budget stopped dispatch), `1` discovery failed, `2` bad input.

| Env | Default | Meaning |
|---|---|---|
| `LINEAGE_FANOUT` | `process` | `process`: one `claude -p` per column via `xargs -P`. `subagent`: the conductor dispatches `column-tracer` subagents in its own session |
| `LINEAGE_CONCURRENCY` | `8` | parallel sessions |
| `LINEAGE_CONDUCTOR_MODEL` / `LINEAGE_TRACER_MODEL` / `LINEAGE_REVIEWER_MODEL` | `opus` / `sonnet` / tracer's | aliases or full IDs |
| `LINEAGE_REVIEW` | `low` | `none`; `low` = review findings with confidence below high or any unresolved; `all` |
| `LINEAGE_CALL_BUDGET` / `LINEAGE_RUN_BUDGET` | `2` / `100` | USD per session (`--max-budget-usd`; conductor gets 4×) and a run ceiling enforced by a flock'd ledger |
| `LINEAGE_ATTEMPTS` / `LINEAGE_MAX_TURNS` / `LINEAGE_TIMEOUT` | `2` / `60` / `900` | per session |
| `LINEAGE_CODE_URL_BASE` | from `origin` + HEAD | override for GHES or non-GitHub hosts |

Re-running with `--run <same name>` skips the manifest, every column with a finding, and every finished review. It refuses if the repo's HEAD moved.

## How it works

```
bin/tracer
 ├─ 0 workspace  clone or use mounted repo; require clean tree; record sha            → run.json
 ├─ 1 discover   claude -p --agent conductor   ──▶ check manifest                     → manifest.json
 ├─ 2 trace      N × claude -p --agent column-tracer ──▶ check finding (retry w/ feedback) → findings/<col>.json
 ├─ 3 review     claude -p --agent lineage-reviewer for selected columns; one repair if rejected → reviews/<col>.json
 └─ 4 assemble   assemble.py                                                           → lineage.csv, dependencies.csv, gaps.md, report.json
```

Stages talk only through files. Every session uses `--json-schema` so the agent's final answer is validated structured output. `bin/check` then re-opens each cited file and rejects the answer if the quoted text is not at the cited lines, or if the graph is malformed (cycle, orphan node, terminal with upstream edges, a value source with no mapping, a join/filter column mapped as a source). Rejections go back to the agent as feedback on the retry.

```
harness/                    copied to /work — the Claude Code project the agents run in
  CLAUDE.md                 principles + shared vocabulary (node, value edge, dependency edge, terminal source, dead end)
  settings.json             read-only permission allowlist, subagent limits
  agents/conductor.md       discovery; runs as the main session via --agent
  agents/column-tracer.md   one column → cited graph; main session (process) or subagent (subagent mode)
  agents/lineage-reviewer.md replays a finding against the source, accepts or rejects with cited issues
  skills/etl-discovery      finding build sites across dialects/notebooks/templating
  skills/lineage-tracing    the hop discipline: value vs dependency, scope, boundaries, fan-in, mapping composition, confidence
  skills/lineage-contract   what each output field means
schemas/                    manifest, finding, review JSON Schema (the --json-schema contracts)
bin/                        tracer, trace-one, review-one, lib/session.sh, check, budget, assemble.py
tests/                      fake claude + 16 tests   (python3 -m unittest discover -s tests -v)
examples/etl/               two-file Teradata fixture (mart.daily_sales) used by the tests and the first live smoke test
docs/evaluation.md          the three gates before trusting output
```

## Design decisions

**The CSV is value lineage.** A row means "this source column's value contributes to this target column". Join keys, filters, group/window/order columns and control flags decide *which rows* arrive, not the value — they are recorded as typed edges in the finding graph and exported to `dependencies.csv`, deduplicated per table, never as rows in `lineage.csv`. Row-selection context still appears in the Mapping Rule text (`… where fx.is_current = 1`) so a reader sees it where it matters. See [`docs/evaluation.md`](docs/evaluation.md) for how this is checked.

**Verify structurally, review selectively.** Citation entailment and graph invariants are free (no model call) and catch the most likely failure — a plausible citation the agent never read. An independent reviewer session catches semantic errors but doubles cost and shares the tracer's blind spots, so it defaults to `low`: only findings that already admit doubt. Change the default once Gate 2 of the evaluation says what it catches.

**Process fan-out by default, subagents one flag away.** Same `column-tracer.md` either way — Claude Code runs an agent file as the main session (`--agent`) or as a subagent (`Agent` tool). Process mode gives each column its own session, budget, log and result file: 200 columns don't share a context window or a concurrency cap, a crash loses one column, and a re-run is a skip.

**Scope, not names.** A temp table written twice is two nodes with different `scope`. The CSV can't show that; the finding JSON can, and that is the artifact a graph or catalog consumer should read.

**Harness-native, no `--bare`.** `--bare` skips `.claude/` discovery, which is where the agents and skills live. Instead `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` leaves only our three agents, and `settings.json` locks tools to read-only on `repo/` plus `rg`/`git log`/`git blame`.

**Skills carry the discipline; agents carry the steps.** Following mattpocock/skills: each agent file is a short ordered sequence with a completion criterion per step; the reusable knowledge lives in model-invoked skills preloaded with the `skills:` frontmatter field. To change how tracing behaves, edit the skill; to change the pipeline, edit the agent.

**Gaps are output.** A dead end becomes a `low` confidence finding with an `unresolved` line, a row with `Source Type: unresolved`, and an entry in `gaps.md`; the file is renamed `.partial` so nobody ships it as complete. The only deterministic code, `assemble.py` and `check`, never reads SQL.
