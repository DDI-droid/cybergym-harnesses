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
PY
)"
prompt="$(cat /harness/prompt.txt)"

export DEEPSEEK_BASE_URL="${OPENAI_BASE_URL}"
export DEEPSEEK_API_KEY="${OPENAI_API_KEY}"

cd /workspace
rm -f /tmp/cybergym_crash_confirmed /tmp/cybergym_submissions.log

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
