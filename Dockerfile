# Thin harness: Claude Code (headless) + the tools an agent needs to read a repo.
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep jq python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && useradd -m -u 1000 lineage

# /work is the Claude Code project root. The target repo is mounted (or cloned) at /work/repo,
# so the harness (.claude/, CLAUDE.md) applies to ANY workspace without writing into it.
WORKDIR /work
COPY --chown=lineage harness/CLAUDE.md   /work/CLAUDE.md
COPY --chown=lineage harness/settings.json /work/.claude/settings.json
COPY --chown=lineage harness/agents      /work/.claude/agents
COPY --chown=lineage harness/skills      /work/.claude/skills
COPY --chown=lineage bin                 /opt/lineage/bin
COPY --chown=lineage schemas             /opt/lineage/schemas
RUN chmod +x /opt/lineage/bin/* && mkdir -p /work/repo /work/.lineage && chown -R lineage /work

USER lineage
ENV CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1 \
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    LINEAGE_FANOUT=process \
    LINEAGE_CONCURRENCY=8 \
    LINEAGE_CONDUCTOR_MODEL=opus \
    LINEAGE_TRACER_MODEL=sonnet \
    LINEAGE_MAX_TURNS=60

ENTRYPOINT ["/opt/lineage/bin/tracer"]
