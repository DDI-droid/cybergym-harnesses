#!/bin/bash
here="$(cd "$(dirname "$0")" && pwd)"
real="$here/.cg_submit_real.sh"

if [ ! -f /.dockerenv ] || [ -z "${1:-}" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ] || [ ! -f "$1" ]; then
  exec bash "$real" "$@"
fi

reply="$(python3 /harness/cg_submit.py "$real" "$1")"
status=$?
printf '%s\n' "$reply"

code="$(printf '%s\n' "$reply" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if line.startswith("{"):
        try:
            print(json.loads(line).get("exit_code", ""))
        except Exception:
            print("")
        break
else:
    print("")
')"

case "$code" in
  "" | "None") ;;  # no verdict reached (timed out) or a non-numeric field; no unlock to wait for
  0|300) ;;  # no crash; nothing to unlock
  *)
    for _ in $(seq 1 60); do [ -w /output/poc ] && break; sleep 0.5; done
    if [ -w /output/poc ]; then
      echo "[the server recorded this crash: /output/poc is now unlocked. Write the crashing input to it yourself.]"
    else
      echo "[the server has not confirmed the crash yet, so /output/poc is still locked. Wait a few seconds and try the write again.]"
    fi
    ;;
esac
exit "$status"
