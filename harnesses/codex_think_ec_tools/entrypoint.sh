#!/usr/bin/env bash
set -u

limit="${CYBERGYM_TIME_LIMIT_S:-3600}"
eval "$(python3 - <<'PY'
import json, os
s = json.loads(os.environ.get("CYBERGYM_HARNESS_SETTINGS") or "{}")
print(f"iterations={int(s.get('max_iterations', 500))}")
print(f"export CG_CONTEXT_LIMIT={int(s.get('context_limit', 163840))}")
print(f"export CG_COMPACT_AT={float(s.get('compact_at', 0.95))}")
print(f"export CG_COMPACT_ROUNDS={int(s.get('compact_rounds', 20))}")
print(f"export CG_KEEP_RECENT_ROUNDS={int(s.get('keep_recent_rounds', 10))}")
print(f"export CG_OUTPUT_RESERVE={int(s.get('output_reserve_tokens', 8192))}")
print(f"export CG_REQUEST_OVERHEAD={int(s.get('request_overhead_tokens', 2000))}")

def cg_timeout_setting(name, default):
    value = s.get(name, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not float(value).is_integer() or value <= 0:
        print(f"echo \"entrypoint: harness setting {name}={value!r} must be a positive whole number of milliseconds\" >&2")
        print("exit 97")
        raise SystemExit
    return int(value)

def cg_submit_setting(name, default):
    value = s.get(name, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        print(f"echo \"entrypoint: harness setting {name}={value!r} must be a positive number of seconds\" >&2")
        print("exit 97")
        raise SystemExit
    return float(value)

tool_default = cg_timeout_setting("tool_timeout_default_ms", 60_000)
tool_max = cg_timeout_setting("tool_timeout_max_ms", 300_000)
submit_timeout_ms = cg_timeout_setting("submit_timeout_ms", 240_000)
if tool_default > tool_max:
    print(f"echo \"entrypoint: tool_timeout_default_ms ({tool_default}) must be <= tool_timeout_max_ms ({tool_max})\" >&2")
    print("exit 97")
    raise SystemExit
print(f"export CG_TOOL_TIMEOUT_DEFAULT_MS={tool_default}")
print(f"export CG_TOOL_TIMEOUT_MAX_MS={tool_max}")
print(f"export CG_SUBMIT_TIMEOUT_MS={submit_timeout_ms}")

submit_budget = cg_submit_setting("submit_budget_s", 200.0)
submit_tool_deadline_s = submit_timeout_ms / 1000.0
if submit_budget >= submit_tool_deadline_s:
    print(f"echo \"entrypoint: submit_budget_s ({submit_budget}) must be less than submit_timeout_ms/1000 ({submit_tool_deadline_s})\" >&2")
    print("exit 97")
    raise SystemExit
print(f"export CG_SUBMIT_BUDGET_S={submit_budget}")
print(f"export CG_SUBMIT_CONNECT_S={cg_submit_setting('submit_connect_s', 10.0)}")
print(f"export CG_SUBMIT_MAX_ATTEMPT_S={cg_submit_setting('submit_max_attempt_s', 120.0)}")
print(f"export CG_SUBMIT_TOOL_DEADLINE_S={submit_tool_deadline_s}")
PY
)"
prompt="$(cat /harness/prompt.txt)"

export DEEPSEEK_BASE_URL="${OPENAI_BASE_URL}"
export DEEPSEEK_API_KEY="${OPENAI_API_KEY}"

# The evaluator hands over /output/poc as an empty file this user cannot write yet, and makes it
# writable only after the submission server records a crash. Never run without that lock.
if [ ! -f /output/poc ] || [ -s /output/poc ] || [ -w /output/poc ]; then
  echo "gate missing: /output/poc must be an empty file the agent cannot write yet; refusing to run" >&2
  exit 97
fi

cd /workspace
rm -f /tmp/cybergym_submissions.log
# CG_DEADLINE_EPOCH: the Unix-seconds instant after which no more tool time is left in this run's own
# agent budget, with the same 30 s safety margin the codex invocation below already reserves for itself.
export CG_DEADLINE_EPOCH=$(( $(date +%s) + limit - 30 ))

restore() {
  if [ -f /workspace/.cg_submit_real.sh ]; then
    mv -f /workspace/.cg_submit_real.sh /workspace/submit.sh
  fi
  cp /tmp/cybergym_submissions.log /output/submissions.log 2>/dev/null
}
trap restore EXIT

mv /workspace/submit.sh /workspace/.cg_submit_real.sh
cp /harness/submit_wrapper.sh /workspace/submit.sh
chmod +x /workspace/submit.sh /workspace/.cg_submit_real.sh

timeout -k 10 "$((limit - 30))" codex --provider deepseek --model "${CYBERGYM_MODEL}" --full-auto --quiet \
  --max-iterations "${iterations}" "${prompt}" > /output/codex.out 2> /output/codex.err
status=$?

rollout="$(ls -t "${HOME}"/.codex/sessions/rollout-*.json 2>/dev/null | head -1)"
if [[ -n "${rollout}" ]]; then
  cp "${rollout}" /output/rollout.json
  python3 /harness/to_trajectory.py /output/rollout.json /output/trajectory.jsonl
elif [[ -s /output/codex.out ]]; then
  python3 /harness/to_trajectory.py /output/codex.out /output/trajectory.jsonl
fi
exit "${status}"
