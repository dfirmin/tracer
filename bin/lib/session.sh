# session.sh — run one headless Claude session with validation and retry-with-feedback.
# Sourced by tracer, trace-one, review-one. Requires: RUN_DIR, WORK, LINEAGE_HOME.
#
#   run_session <label> <agent> <model> <schema-file> <check-kind> <column-or-empty> <prompt> <out-file>
# Writes the validated structured output to <out-file>. Returns 0 on success, 1 after
# LINEAGE_ATTEMPTS failures (last problems in $RUN_DIR/logs/<label>.problems).
run_session() {
  local label="$1" agent="$2" model="$3" schema="$4" kind="$5" column="$6" prompt="$7" out="$8"
  local log="$RUN_DIR/logs/$label" feedback="" attempt
  for attempt in $(seq 1 "${LINEAGE_ATTEMPTS:-2}"); do
    "$LINEAGE_HOME/bin/budget" reserve "$label#$attempt" || return 3
    timeout --kill-after=30 "${LINEAGE_TIMEOUT:-900}" \
      claude -p "$prompt$feedback" \
        --agent "$agent" --model "$model" \
        --permission-mode dontAsk --max-turns "${LINEAGE_MAX_TURNS:-60}" \
        --max-budget-usd "${LINEAGE_CALL_BUDGET:-2}" \
        --output-format json --json-schema "$(cat "$schema")" \
        > "$log.$attempt.json" 2> "$log.$attempt.stderr"
    local cost; cost=$(jq -r '.total_cost_usd // "unknown"' "$log.$attempt.json" 2>/dev/null || echo unknown)
    "$LINEAGE_HOME/bin/budget" settle "$label#$attempt" "$cost"
    if ! jq -e '.structured_output' "$log.$attempt.json" > "$out.tmp" 2>/dev/null; then
      feedback=$'\n\nYour previous attempt returned no structured output (see the harness error). Return the JSON described by the schema as your final answer.'
      continue
    fi
    local problems
    if problems=$("$LINEAGE_HOME/bin/check" "$kind" "$out.tmp" ${column:+--column "$column"} --work "$WORK"); then
      mv "$out.tmp" "$out"; rm -f "$log.problems"; return 0
    fi
    printf '%s\n' "$problems" > "$log.problems"
    feedback=$'\n\nThe harness rejected your previous answer. Fix every item below and return the corrected JSON. Each rejected citation means the quote did not occur at those lines: re-read the file and cite the exact text.\n'"$problems"
  done
  rm -f "$out.tmp"; return 1
}
