# session.sh — run one headless Claude session with validation and retry-with-feedback.
# Sourced by tracer, trace-one, review-one. Requires: RUN_DIR, WORK, LINEAGE_HOME.
#
#   run_session <label> <agent> <model> <schema-file> <check-kind> <column-or-empty> <prompt> <out-file>
# Writes the validated structured output to <out-file>. Returns 0 on success, 1 after
# LINEAGE_ATTEMPTS failures (last problems in $RUN_DIR/logs/<label>.problems), 3 when the
# run budget refuses a reservation.
#
# Every claude call is one line in $RUN_DIR/attempts.jsonl (read by bin/metrics), and keeps
# its envelope and stderr at logs/<label>.<n>.json / .stderr. Three kinds of retry are kept apart:
#   transient   the gateway/API failed (429, 5xx, overload, timeout, connection) — retried after
#               backoff with the same prompt; does not use up one of LINEAGE_ATTEMPTS
#   structural  the answer was missing or failed bin/check — retried with the exact problems as feedback
#   (semantic   a reviewer rejected a valid finding — review-one's repair-/rereview- sessions)

TRANSIENT_RE='(\b429\b|rate.?limit|overloaded|\b5[0-9][0-9]\b|ECONNRESET|ETIMEDOUT|ECONNREFUSED|socket hang up|timed? ?out|Service Unavailable)'

_attempt_record() {  # label agent n class outcome rc started finished envelope stderr
  local label="$1" agent="$2" n="$3" class="$4" outcome="$5" rc="$6" t0="$7" t1="$8" env="$9" err="${10}"
  local usage='{}' turns=null cost=null
  if jq -e . "$env" >/dev/null 2>&1; then
    usage=$(jq -c '.usage // {}' "$env"); turns=$(jq '.num_turns // null' "$env"); cost=$(jq '.total_cost_usd // null' "$env")
  fi
  local n429; n429=$(grep -c -i -E '\b429\b|rate.?limit' "$err" 2>/dev/null || true)
  jq -nc --arg label "$label" --arg agent "$agent" --argjson n "$n" --arg class "$class" --arg outcome "$outcome" \
    --argjson rc "$rc" --argjson t0 "$t0" --argjson t1 "$t1" --argjson usage "$usage" --argjson turns "$turns" \
    --argjson cost "$cost" --argjson n429 "${n429:-0}" \
    '{label:$label, agent:$agent, attempt:$n, class:$class, outcome:$outcome, exit_code:$rc,
      started:$t0, finished:$t1, duration_s:(($t1-$t0)*1000|round/1000), num_turns:$turns, cost_usd:$cost,
      input_tokens:($usage.input_tokens // null), output_tokens:($usage.output_tokens // null),
      cache_read_tokens:($usage.cache_read_input_tokens // null), cache_write_tokens:($usage.cache_creation_input_tokens // null),
      rate_limit_hits:$n429}' >> "$RUN_DIR/attempts.jsonl"
}

_now() { date +%s.%N | cut -c1-14; }

run_session() {
  local label="$1" agent="$2" model="$3" schema="$4" kind="$5" column="$6" prompt="$7" out="$8"
  local log="$RUN_DIR/logs/$label" feedback="" attempt=0 n=0 transient=0 class="first"
  local max_attempts="${LINEAGE_ATTEMPTS:-2}" max_transient="${LINEAGE_TRANSIENT_RETRIES:-3}"
  while (( attempt < max_attempts )); do
    n=$((n + 1))
    "$LINEAGE_HOME/bin/budget" reserve "$label#$n" || return 3
    local t0 t1 rc=0; t0=$(_now)
    timeout --kill-after=30 "${LINEAGE_TIMEOUT:-900}" \
      claude -p "$prompt$feedback" \
        --agent "$agent" --model "$model" \
        --permission-mode dontAsk --max-turns "${LINEAGE_MAX_TURNS:-60}" \
        --max-budget-usd "${LINEAGE_CALL_BUDGET:-2}" \
        --output-format json --json-schema "$(cat "$schema")" \
        > "$log.$n.json" 2> "$log.$n.stderr" || rc=$?
    t1=$(_now)
    local cost; cost=$(jq -r '.total_cost_usd // "unknown"' "$log.$n.json" 2>/dev/null || echo unknown)
    "$LINEAGE_HOME/bin/budget" settle "$label#$n" "$cost"

    if ! jq -e '.structured_output' "$log.$n.json" > "$out.tmp" 2>/dev/null; then
      # no answer: transient if the call itself failed in a way a retry can fix
      if (( rc == 124 || rc == 137 )) || { (( rc != 0 )) && grep -q -i -E "$TRANSIENT_RE" "$log.$n.stderr" "$log.$n.json" 2>/dev/null; }; then
        _attempt_record "$label" "$agent" "$n" "$class" transient_failure "$rc" "$t0" "$t1" "$log.$n.json" "$log.$n.stderr"
        if (( transient < max_transient )); then
          transient=$((transient + 1)); class="transient"
          local wait; wait=$(python3 -c "import random;b=float('${LINEAGE_BACKOFF_BASE:-5}');print(round(min(b*2**($transient-1),120)*(0.5+random.random()/2),2))")
          sleep "$wait"; continue
        fi
      else
        _attempt_record "$label" "$agent" "$n" "$class" no_output "$rc" "$t0" "$t1" "$log.$n.json" "$log.$n.stderr"
      fi
      attempt=$((attempt + 1)); class="structural"
      feedback=$'\n\nYour previous attempt returned no structured output (see the harness error). Return the JSON described by the schema as your final answer.'
      continue
    fi
    local problems
    if problems=$("$LINEAGE_HOME/bin/check" "$kind" "$out.tmp" ${column:+--column "$column"} --work "$WORK"); then
      _attempt_record "$label" "$agent" "$n" "$class" ok "$rc" "$t0" "$t1" "$log.$n.json" "$log.$n.stderr"
      mv "$out.tmp" "$out"; rm -f "$log.problems"; return 0
    fi
    _attempt_record "$label" "$agent" "$n" "$class" rejected "$rc" "$t0" "$t1" "$log.$n.json" "$log.$n.stderr"
    printf '%s\n' "$problems" > "$log.problems"
    attempt=$((attempt + 1)); class="structural"
    feedback=$'\n\nThe harness rejected your previous answer. Fix every item below and return the corrected JSON. Each rejected citation means the quote did not occur at those lines: re-read the file and cite the exact text.\n'"$problems"
  done
  rm -f "$out.tmp"; return 1
}

# stage <name> start|end — append to the run's timeline (read by bin/metrics)
stage() {
  jq -nc --arg s "$1" --arg e "$2" --arg inv "${LINEAGE_INVOCATION:-0}" --argjson at "$(_now)" \
    '{invocation:$inv, stage:$s, event:$e, at:$at}' >> "$RUN_DIR/timeline.jsonl"
}
