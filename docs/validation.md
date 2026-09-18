# Validation record

Date: 2026-09-18.

- Python 3.12: all 19 unittest cases passed. Includes a 205-column simulated run (412 fake invocations), bounded concurrency, checkpoint reuse, failed-column recovery, bounded semantic repair, changed-context and changed-snapshot rejection, literal Git blob preservation, budget reservations, evidence/graph validation, CSV round-trip and process-group timeout cleanup.
- All three SKILL.md files passed the skill-creator structural validator.
- Python modules compiled and CLI help rendered successfully.
- An independent agent used the tracing skill on the two-file synthetic Teradata example. Its net_usd trace contained 15 nodes, 16 edges and 12 terminal mappings. Source citation and graph validators accepted it. Manual inspection confirmed amount, discount, zero default, rate, paid status, both join keys, current-rate condition and their literals were represented. This was not a Claude Code run.
- Docker build and real Claude API execution were not performed: neither Docker nor the Claude CLI was available in the implementation workspace. There is no production-repository accuracy result yet.

The unit tests validate orchestration and contracts with fake model outputs. They cannot establish real model accuracy, latency, API cost or compatibility of the complete container at runtime. Complete the live and semantic gates in evaluation.md before production use.
