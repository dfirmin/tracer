# Repository Lineage

Generate source-to-target mappings by having Claude Code read and reason about a committed ETL repository. There is no SQL parser, AST, dialect adapter, ETL execution, or database connection. Python handles inputs, scheduling, validation, recovery and CSV serialization only.

This is a working reference implementation with an offline-tested runner. A live Claude/Docker smoke test and evaluation against your actual ETL repositories are still required before trusting production mappings.

## How it works

1. A conductor session finds the target's writers, execution context and complete ordered column list. An independent session verifies that discovery.
2. The runner dispatches one isolated headless Claude session per target column, up to the concurrency limit. Each worker reads the same immutable repository snapshot and discovery context.
3. Workers follow values and row-selection dependencies through all available intermediate objects. A second session reviews each trace against source. A rejected trace gets one semantic repair and another review.
4. Structural checks validate evidence, connected acyclic graphs, terminal boundaries and flattened source coverage. The runner exports the ten requested fields. Failed columns remain explicit in the coverage report.

These workers are separately supervised Claude processes, rather than native Agent-tool children of one long-lived Claude conversation. The conductor decides the work by producing the verified column manifest; Python dispatches it. This keeps retries, concurrency and checkpoints outside model context. One container runs the supervisor and its child sessions. Multiple table jobs can run in separate containers; no Docker socket is mounted.

## Run with Docker

Requirements: Docker with Compose, a clean committed Git checkout, and an Anthropic API key. Clone private repositories using your existing host Git credentials; the container reads the checkout and needs no GitHub token. The supplied repository URL must refer to that same repository, and the input commit must be pushed for citations to resolve.

The following shell commands work in Linux, macOS or WSL. Replace the paths, repository URL and target identifiers with your values.

```bash
export TARGET_WORKSPACE=/absolute/path/to/your-etl-repo
export LINEAGE_OUTPUT=/absolute/path/to/lineage-output
export TARGET_REPO_URL=https://github.com/your-org/your-etl-repo
export TARGET_SCHEMA=mart
export TARGET_TABLE=daily_sales
export ANTHROPIC_API_KEY='your-key'
mkdir -p "$LINEAGE_OUTPUT"
# The image runs as UID 1000. On Linux, grant that user write access to this directory.
docker compose build
docker compose run --rm lineage
```

Do not commit your key. Container outbound access to the Anthropic API is required. The CLI is pinned to `2.1.276` (verified release on 2026-09-18); override `CLAUDE_CODE_VERSION` deliberately when upgrading and repeat the live smoke test. The base Node image uses a moving tag: pin its digest in your deployment if you need byte-for-byte image reproducibility. `--model` also accepts an exact provider model ID instead of the moving `sonnet` default.

For environment values, job selection or an independently supplied target schema, place a text context file and an ordered JSON column list in your output directory before running. Context should contain configuration values, not credentials. Override the Compose command:

```bash
docker compose run --rm lineage \
  --workspace /workspace \
  --repo-url "$TARGET_REPO_URL" \
  --schema "$TARGET_SCHEMA" --table "$TARGET_TABLE" \
  --out /output \
  --context /output/context.txt --columns /output/columns.json \
  --concurrency 4 --call-budget 2 --run-budget 100
```

The optional column manifest looks like `["order_id", "net_usd", "segment"]`. The agent still has to establish those columns from repository evidence. If the source depends on a runtime-only catalog to expand a wildcard, discovery stops instead of inventing columns.

For a local run with Python 3.11+ and the pinned Claude CLI installed, use the same arguments:

```bash
python3 -m lineage.runner --workspace /path/to/etl \
  --repo-url https://github.com/your-org/etl \
  --schema mart --table daily_sales --out /path/to/results
```

No Python packages need installing. `fcntl` makes the runner Linux/macOS oriented; use Docker/WSL on Windows. The default authentication route is an API key, not a Claude subscription login.

## Outputs

| File | Meaning |
| --- | --- |
| `mapping.csv` | Requested target columns mapped to every terminal source, with the composed derivation and all relevant citations |
| `lineage.csv` | Every immediate source-to-target hop, including temporary and intermediate physical objects |
| `lineage.json` | Full per-column graphs, scoped object identities, dependency kinds, evidence, boundaries and reviews |
| `report.json` | Per-column coverage, gaps, completeness status and reported spend |
| `discovery.json` | Verified column manifest and shared execution context |
| `columns/` | Atomically saved per-column checkpoints |
| `logs/` | Raw CLI results, error details, returned structured outputs and invocation metadata |
| `budget.json` | Persisted spend reports and outstanding/unknown-cost reservations |
| `input/` | Exact committed file snapshot used by all sessions |

Both CSVs use precisely these headers:

```text
Target Schema,Target Table,Target Column,Source Schema,Source Table,Source Column,Source Type,Mapping Rule,Mapping Business Rule,Code URL
```

Mapping CSV rows keep the originally requested table as Target. Hop CSV rows use each immediate intermediate object as Target. Source Type describes the source object (`physical`, `view`, `temp`, `cte`, `dataframe`, `file`, `literal`, `runtime`, or `unresolved`). Dependency roles appear as prefixes such as `[join]` or `[filter]` in Mapping Business Rule because the requested schema has no dedicated role column. A source can have multiple rows when branches or roles differ.

Mapping Rule may contain an explicitly labeled ordered derivation, rather than fabricated runnable SQL combining multiple dialects. Code URL is a ` | ` separated list of commit-pinned GitHub line links. Quotes and multiline expressions are preserved by standard CSV escaping. Treat CSV cells as text when importing into spreadsheets: preserving source expressions means formulas are not rewritten for spreadsheet safety.

The JSON graph is authoritative for scope: the ten CSV columns cannot uniquely distinguish temporary names reused across jobs or write versions, nor encode arbitrary branching. Hop rows are intentionally not deduplicated across root-column traces. A graph or BI consumer should retain each trace's root and node scope from JSON.

## Completeness and failure behavior

`complete_within_repo` means every discovered target column has a reviewed, structurally valid trace terminating at supported repository boundaries, constants or known runtime values. It is not a guarantee of model correctness or ultimate system-of-record lineage. Repository-bounded sources have cited use and producer-search notes.

Missing temporary producers, unresolved templates, unknown UDF bodies, historical cycles and dynamic source selection stay unresolved. A reviewer can accept an accurately disclosed partial graph without marking it complete. If any column fails, remains unresolved or fails review, only `mapping.partial.csv` and `lineage.partial.csv` are emitted. Failed/rejected traces are excluded from those CSVs but appear in the report; candidate rejected traces are retained separately for diagnosis. Incomplete discovery stops before dispatch.

Exit codes: `0` means complete within the repo; `2` means column-level partial/failed results; `1` means a preflight/discovery/run error. Error details are printed to stderr, with agent-call diagnostics retained in logs when a call occurred.

Run the same command and output directory to resume. Complete reviewed columns are reused; partial or failed columns run again. The commit, snapshot content, target, execution context, expected columns, model, CLI version, runtime code and skill content must match. Changing any of those requires a new output directory. A discovery review rejection is retained and needs a new run with resolved context. A concurrent process cannot use the same output directory. Budget or concurrency limits can change on resume.

## Cost and scale

Defaults: 4 concurrent sessions, 60 turns/call, 15 minutes/call, USD 2/call, USD 100/run scheduling ceiling, 2 attempts/stage. Each fresh invocation reserves its full per-call cap before dispatch. A successful call replaces the reservation with the CLI's reported spend. Unknown-cost failures keep the full reservation, including after a restart; raise `--run-budget` deliberately to continue. These are scheduling controls and CLI estimates, not a billing guarantee: provider accounting and cap overshoot can differ.

A clean run for N columns uses `2 + 2N` sessions: discovery, discovery review, N traces and N reviews. A semantic repair adds two stages for that column; every stage can retry up to `--attempts`. For 200 columns, plan for at least 402 sessions, with actual cost determined by repository size, model, caching and trace depth. No blanket cost or latency estimate is implied by the defaults. Start with a small known target and inspect spend before increasing the run ceiling.

Workers share discovery context, not a writable cross-column fact cache. This avoids silently accepting another worker's unreviewed derivation, but repeats upstream reading. A future cache should key reviewed facts by commit, execution context, scoped object/column, skill version and model, and retain evidence. Do not cache bare SQL identifier strings across jobs.

## Input and security scope

The repository is mounted read-only. CLI sessions use bare/restricted mode and only Read/Glob/Grep tools, with no Bash, edits, network tools, MCP servers, hooks or auto-discovered target instructions. Only this project's skill text is explicitly injected. Output directories contain source excerpts; apply your own retention/access policy.

This supports varied repository text without adding dialect adapters; it cannot guarantee resolution for every workspace. Symlinks, submodules, uncommitted changes, non-UTF-8 source citations and runtime-only metadata currently require preparation or additional input. Git LFS pointers and encrypted/binary notebooks do not expose their underlying code; supply the actual source as committed text. Large repositories are copied as Git blobs in memory, so size the container accordingly. The container is not a security boundary between trusted supervisor code and API credentials; use a dedicated job environment and do not mount unrelated secrets.

## Skills and maintenance

Skills live in `skills/<name>/SKILL.md` (singular, the harness convention), with compact task-specific guidance and a linked semantic reference. The runner injects each selected skill explicitly for headless reproducibility. They are project assets, not installed personal ChatGPT skills.

The design follows the concise, task-focused instruction and small-interface approach illustrated by [Matt Pocock's skills](https://github.com/mattpocock/skills), particularly [research](https://github.com/mattpocock/skills/blob/main/skills/engineering/research/SKILL.md) and [codebase-design](https://github.com/mattpocock/skills/blob/main/skills/engineering/codebase-design/SKILL.md). The lineage guidance is original to this project.

Harness behavior is based on Anthropic's [headless usage](https://code.claude.com/docs/en/headless) and [CLI reference](https://code.claude.com/docs/en/cli-reference). Version provenance: [v2.1.276 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.276).

## Validation

```bash
python3 -m unittest discover -s tests -v
```

The transport tests simulate CLI responses and exercise a 205-column run, bounded concurrency, independent recovery, semantic repair, budget reservations, source/citation guards, CSV round-tripping and immutable inputs. They do not measure Claude's lineage accuracy. An independent agent also traced the synthetic two-file example; that is a prompt behavior check, not a Claude Code integration test.

For the first live smoke test, use a clean committed checkout containing `examples/etl/`, target `mart.daily_sales`, and supply the example execution context/column list. Check net_usd against the raw amount/discount/rate derivation and its paid/current-rate filters and customer/currency joins. Then follow the evaluation plan in `docs/evaluation.md` before production adoption.
