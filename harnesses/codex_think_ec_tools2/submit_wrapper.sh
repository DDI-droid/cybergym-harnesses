#!/bin/bash
# /workspace/submit.sh in the agent container (the entrypoint moves the generated script to .cg_submit_real.sh).
# Routes a submission through the typed client; everything else falls through to the generated script.
#
# FRONT GATE (this file, before any Python starts): at most CG_SUBMIT_MAX_CONCURRENT (default 3) submit clients run at once in
# this container. Each client holds a non-blocking flock on one of N slot files (/tmp/cg_submit_slot.<i>); a caller that finds
# every slot taken does NOT wait and does NOT start Python: it prints one typed JSON line (status "busy", nothing sent, nothing
# evaluated), appends a "busy" record to /output/submissions.jsonl and exits 3. Cost of a refused caller: this bash (~3.5 MB)
# plus a short-lived util-linux `flock` per slot tried.
#   Why 3: the allowance is 18 requests per 60 s = 0.3/s; a verdict takes up to ~10 s, so by Little's law 0.3/s x 10 s = 3 clients
#   in flight saturate it. More concurrent clients only wait. 3 clients x ~14 MB (python) stays under 50 MB.
#
# ADVISORY: the gate runs INSIDE the agent's container, so it limits only callers that go through this file. The agent can bypass
# it (and the typed client, its pacing and its history) with raw curl or by running the hidden generated script
# .cg_submit_real.sh. The hard limit on what a container can do is the process cap the framework sets (task_pids_limit).
#
# Exit status: the client's (0 verdict, 1 other typed status, 2 refusal, 143 cancelled), or 3 = busy (this file), 2 = this
# file's own refusal (bad CG_SUBMIT_MAX_CONCURRENT, no flock, slot directory not writable).
trap '' PIPE
here="$(cd "$(dirname "$0")" && pwd)"
real="$here/.cg_submit_real.sh"
client="${CG_SUBMIT_CLIENT:-/harness/cg_submit.py}"

if [ "${1:-}" = "--history" ]; then
  exec python3 "$client" "$@"
fi

if { [ ! -f /.dockerenv ] && [ -z "${CG_SUBMIT_FORCE_CLIENT:-}" ]; } || [ -z "${1:-}" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ] || [ ! -f "$1" ]; then
  bash "$real" "$@"
  status=$?
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    echo "history: submit.sh --history [N] [--offset M] [--kind results|attempts|busy|all]  (full record: /output/submissions.jsonl)"
    echo "at most ${CG_SUBMIT_MAX_CONCURRENT:-3} submissions run at once; a further call returns status \"busy\" (exit 3) at once and sends nothing: wait for the running ones"
  fi
  exit "$status"
fi

# ---- front gate -------------------------------------------------------------------------------------------------------------
max="${CG_SUBMIT_MAX_CONCURRENT:-3}"
slot_dir="${CG_SUBMIT_SLOT_DIR:-/tmp}"
log="${CG_SUBMISSIONS_JSONL:-/output/submissions.jsonl}"
inv="${CG_INVOCATION_ID:-}"
case "$inv" in *[!A-Za-z0-9._-]*) inv="" ;; esac
if [ -n "$inv" ]; then inv_json="\"$inv\""; else inv_json=null; fi
max_json=null

typed() {  # status reason message exit_code   (one JSON line; every value is a constant or sanitized above)
  printf '{"status":"%s","executed":false,"sent":false,"candidate_sha256":null,"http_status":null,"exit_code":null,"attempts":0,"elapsed_s":0,"invocation_id":%s,"reason":"%s","max_concurrent":%s,"client_version":"2-gate","message":"%s"}\n' \
    "$1" "$inv_json" "$2" "$max_json" "$3" 2> /dev/null
  exit "$4"
}

case "$max" in
  '' | *[!0-9]* | 0 | 0[0-9]*) typed invalid_request bad_CG_SUBMIT_MAX_CONCURRENT "CG_SUBMIT_MAX_CONCURRENT must be a whole number from 1 to 32; nothing was sent" 2 ;;
esac
if [ "$max" -gt 32 ]; then typed invalid_request bad_CG_SUBMIT_MAX_CONCURRENT "CG_SUBMIT_MAX_CONCURRENT must be a whole number from 1 to 32; nothing was sent" 2; fi
max_json="$max"
command -v flock > /dev/null 2>&1 || typed invalid_request no_flock "flock is not installed in this container, so the submission gate cannot work; nothing was sent" 2
{ [ -d "$slot_dir" ] && [ -w "$slot_dir" ]; } || typed invalid_request slot_dir_not_writable "the submission slot directory is not writable; nothing was sent" 2

i=1
while [ "$i" -le "$max" ]; do
  exec {slot}>> "$slot_dir/cg_submit_slot.$i" 2> /dev/null || typed invalid_request slot_file_unusable "the submission slot file could not be opened; nothing was sent" 2
  if flock -n "$slot"; then
    # lock held by this open file description; the exec'd client inherits it, so it is released when the client exits (or is killed)
    exec python3 "$client" "$real" "$1"
  fi
  exec {slot}>&-
  i=$((i + 1))
done

# every slot is taken: refuse at once, record it, never wait, never start Python
dir="${log%/*}"
if [ -d "$dir" ] && [ -w "$dir" ]; then
  IFS= read -r uid < /proc/sys/kernel/random/uuid 2> /dev/null || uid="$RANDOM$RANDOM$RANDOM"
  TZ=UTC0 printf -v ts '%(%Y-%m-%dT%H:%M:%SZ)T' -1
  now="${EPOCHREALTIME/,/.}"
  printf '{"id":"%s","ts":"%s","t":%s,"kind":"busy","invocation_id":%s,"reason":"too_many_concurrent_submissions","max_concurrent":%s,"pid":%s}\n' \
    "${uid//-/}" "$ts" "${now:-0}" "$inv_json" "$max" "$$" >> "$log" 2> /dev/null
fi
typed busy too_many_concurrent_submissions "$max submissions from this container are already running (limit $max). Nothing was sent and this candidate was not evaluated. Wait for them to finish, then call again; do not start submissions in parallel." 3
