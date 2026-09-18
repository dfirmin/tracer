# tracer

Produces a source-to-target mapping CSV for one table/view by **reading** the ETL repo that builds it. No SQL parser, no per-dialect adapters: a conductor discovers how the target is built, one tracer per column walks the derivation hop by hop to its terminal sources, and a small script joins the findings into a CSV with a code citation on every row.

Works against any workspace: the harness (`CLAUDE.md`, agents, skills) lives at the container's project root and the target repo is mounted beneath it at `repo/`, so nothing is written into the repo and no repo-specific setup is needed.

## Run

```bash
docker build -t tracer .

# repo already on disk
docker run --rm \
  -e ANTHROPIC_API_KEY \
  -v /path/to/etl-repo:/work/repo:ro \
  -v "$PWD/out":/work/.lineage \
  tracer edw.claim_fact

# or clone inside the container
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD/out":/work/.lineage \
  tracer edw.claim_fact --repo git@github.com:org/etl.git --ref main
```

Output: `out/<run>/lineage.csv`, `gaps.md`, `manifest.json`, `findings/*.json`, `logs/`.

Through an internal gateway instead of the public API: set `ANTHROPIC_BASE_URL` (and whatever auth header/env the gateway needs) on the container; Claude Code reads them. For Bedrock, pass the usual `CLAUDE_CODE_USE_BEDROCK=1` and AWS credentials.

| Env | Default | Meaning |
|---|---|---|
| `LINEAGE_FANOUT` | `process` | `process`: one `claude -p` session per column, run by the harness with `xargs -P`. `subagent`: the conductor dispatches `column-tracer` subagents inside its own session |
| `LINEAGE_CONCURRENCY` | `8` | parallel tracer sessions in `process` mode |
| `LINEAGE_CONDUCTOR_MODEL` / `LINEAGE_TRACER_MODEL` | `opus` / `sonnet` | model aliases or full IDs |
| `LINEAGE_MAX_TURNS` | `60` | per tracer; conductor gets 3× |
| `LINEAGE_CODE_URL_BASE` | derived from `origin` + HEAD | override for GHES or non-GitHub hosts (`https://ghe.corp/org/repo/blob/<sha>`) |

Re-running with `--run <same name>` skips the manifest and any column that already has a finding, so a failed or budget-capped run resumes where it stopped.

## How it works

```
bin/tracer
 ├─ 0 workspace   clone or use mounted repo; record sha + code URL base           → run.json
 ├─ 1 discover    claude -p --agent conductor  (skills: etl-discovery, contract)  → manifest.json
 ├─ 2 trace       N × claude -p --agent column-tracer (skills: tracing, contract) → findings/<col>.json
 └─ 3 assemble    assemble.py joins manifest + findings                           → lineage.csv, gaps.md
```

Stages talk only through files. Each `claude -p` call uses `--json-schema` so the agent's final answer is validated structured output, not free text that a script has to parse.

```
harness/                    copied to /work — the Claude Code project the agents run in
  CLAUDE.md                 principles + shared vocabulary (hop, terminal source, dead end)
  settings.json             read-only permission allowlist, subagent limits
  agents/conductor.md       discovery; runs as the main session via --agent
  agents/column-tracer.md   one column → finding; main session (process) or subagent (subagent mode)
  skills/etl-discovery      how to find build sites across dialects/notebooks/templating
  skills/lineage-tracing    the hop discipline, fan-in rules, mapping composition, confidence rubric
  skills/lineage-contract   what each output field means
schemas/                    manifest + finding JSON Schema (the --json-schema contract)
bin/                        tracer (driver), trace-one (per-column session), assemble.py (JSON→CSV)
```

## Design decisions

**Process fan-out is the default, subagent fan-out is one flag away.** Both use the same `column-tracer.md` — Claude Code runs an agent file either as the main session (`--agent`) or as a subagent (`Agent` tool). Process mode is the default because each column then gets its own session, its own budget, its own log, and its own result file on disk: 200 columns don't share a context window or a concurrency cap, a crash loses one column, and a re-run is a `skip`. In headless mode the conductor also has to be trusted to block on every background subagent before it ends its turn; a file per column makes the completion criterion checkable from outside instead.

**No `--bare`.** `--bare` skips `.claude/` discovery, which is where the agents and skills live. The harness instead sets `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` so only `conductor` and `column-tracer` exist, and locks permissions with `dontAsk` plus an allowlist (read-only on `repo/`, write only under `.lineage/`).

**Skills carry the discipline; agents carry the steps.** Following the pattern in mattpocock/skills: each agent file is a short ordered sequence with a completion criterion per step; the reusable knowledge (how to search a mixed-shape repo, how to take a hop, what a field means) lives in model-invoked skills that are preloaded into the agent with the `skills:` frontmatter field. To change how tracing behaves, edit the skill; to change the pipeline, edit the agent.

**Gaps are output, not failures.** A dead end (runtime-computed name, missing `%run` target, UDF body not in the repo) becomes a row with `Source Type: unknown`, a `low` confidence, and a reason in `gaps.md`. The CSV is trustworthy where it is confident and honest where it is not.

**The only deterministic code never sees SQL.** `assemble.py` reshapes JSON the agents produced. Adding a new repo shape means the skill gets a paragraph, not the pipeline a parser.

## Next steps worth doing

- **Verifier stage**: a third agent that samples rows from `lineage.csv`, opens each Code URL's file at the cited lines, and confirms the expression supports the claim. Cheap insurance before the CSV leaves the run.
- **Fixture repo + `scripts/test.sh`**: a tiny mixed-shape repo (one BTEQ script, one SQL notebook, one PySpark file with an f-string table name, one YAML config) with a known-good CSV to diff against.
- **Per-repo `LINEAGE.md`**: if a repo has conventions no skill can guess (naming schemes, which schema is "raw"), mount it at `/work/repo-notes.md` and point `CLAUDE.md` at it.
