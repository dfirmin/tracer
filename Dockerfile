FROM node:22-bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.276
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
WORKDIR /opt/lineage
COPY lineage ./lineage
COPY skills ./skills
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DISABLE_AUTOUPDATER=1
USER node
ENTRYPOINT ["python3", "-m", "lineage.runner"]
