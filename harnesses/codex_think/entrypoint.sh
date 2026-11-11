#!/usr/bin/env bash
set -u

limit="${CYBERGYM_TIME_LIMIT_S:-3600}"
iterations="$(python3 -c 'import json, os; print(json.loads(os.environ.get("CYBERGYM_HARNESS_SETTINGS") or "{}").get("max_iterations", 100))')"
prompt="$(cat /harness/prompt.txt)"

export DEEPSEEK_BASE_URL="${OPENAI_BASE_URL}"
export DEEPSEEK_API_KEY="${OPENAI_API_KEY}"

cd /workspace
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
