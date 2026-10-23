#!/usr/bin/env bash
# Teradata staging for claim_fact. Usage: run_stage.sh [prod|dev]
set -euo pipefail
ENV="${1:-prod}"
if [ "$ENV" = "dev" ]; then
  export STG_DB=STG_DEV RAW_DB=RAW_CLAIMS_DEV
else
  export STG_DB=STG RAW_DB=RAW_CLAIMS
fi
envsubst < "$(dirname "$0")/../bteq/stage_claim_lines.bteq" | bteq
