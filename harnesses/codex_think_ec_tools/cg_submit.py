"""Budgeted, classifying driver for the generated submission script.

usage: cg_submit.py <real_script_path> <poc_path>

Never reimplements trust: it only invokes the real, generated script (which already carries the
server URL and the signed metadata), adding bounded timeouts and retry/backoff around it.

Env (all optional; invalid values are a hard failure, not a silent fallback):
  CG_SUBMIT_BUDGET_S        total wall-clock budget for this one tool call (default 200)
  CG_SUBMIT_CONNECT_S       curl connect-phase timeout (default 10)
  CG_SUBMIT_MAX_ATTEMPT_S   longest any single attempt may run (default 120)
  CG_SUBMIT_TOOL_DEADLINE_S the surrounding tool-call deadline this budget must stay under (default 240)
  CG_SUBMISSIONS_LOG        where to log each attempt (default /tmp/cybergym_submissions.log)

Exit code: 0 if a verdict (crash, no-crash, or server-side timeout 300) was obtained; 1 on budget exhaustion.
Prints the server's own last JSON reply to stdout, exactly as the generated script would, so the model's
existing JSON parsing keeps working; additional lines (history, rate-limit/timeout notices) are appended after it.
"""
import json
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import time

RATE_LIMIT_PATTERN = re.compile(r"rate limit", re.IGNORECASE)
CURL_LINE = re.compile(r"^curl -X POST\b", re.MULTILINE)


def env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        sys.exit(f"invalid {name}={raw!r}: not a number")
    if not (value > 0) or value != value or value in (float("inf"), float("-inf")):  # NaN-safe, rejects inf
        sys.exit(f"invalid {name}={raw!r}: must be a finite number greater than 0")
    return value


def main() -> int:
    real_script, poc_path = sys.argv[1], sys.argv[2]
    budget = env_positive_float("CG_SUBMIT_BUDGET_S", 200.0)
    connect_timeout = env_positive_float("CG_SUBMIT_CONNECT_S", 10.0)
    max_attempt = env_positive_float("CG_SUBMIT_MAX_ATTEMPT_S", 120.0)
    tool_deadline = env_positive_float("CG_SUBMIT_TOOL_DEADLINE_S", 240.0)
    log_path = os.environ.get("CG_SUBMISSIONS_LOG", "/tmp/cybergym_submissions.log")
    if budget >= tool_deadline:
        sys.exit(f"invalid configuration: CG_SUBMIT_BUDGET_S ({budget}) must be less than "
                  f"CG_SUBMIT_TOOL_DEADLINE_S ({tool_deadline}); refusing to start")

    patched_script = _inject_curl_timeouts(real_script, connect_timeout, max_attempt)

    started = time.monotonic()
    attempts = 0
    max_attempts = int(env_positive_float("CG_SUBMIT_MAX_ATTEMPTS", 8.0))  # one tool call never makes more HTTP attempts than this
    limited_streak = transient_streak = 0
    last_reply: dict | None = None
    notices: list[str] = []  # every notice is kept, even once a later attempt succeeds: the model should
                              # see it was rate-limited/retried on the way to a verdict, not just the verdict.
    while True:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 1 or attempts >= max_attempts:
            break
        attempt_timeout = min(max_attempt, remaining)
        attempts += 1
        attempt_started = time.monotonic()
        try:
            raw, returncode = _run_bounded(["bash", patched_script, poc_path], attempt_timeout)
        except OSError as error:
            raw, returncode = f"could not start the submission attempt: {error}", -1
        completed = type("R", (), {"returncode": returncode})()
        attempt_seconds = time.monotonic() - attempt_started
        cleanup_unconfirmed = "CG_SUBMIT_CLEANUP_UNCONFIRMED" in raw
        if cleanup_unconfirmed:
            raw = raw.replace("CG_SUBMIT_CLEANUP_UNCONFIRMED", "")
        reply = _parse_json_line(raw)
        count = _log_count(log_path)

        if cleanup_unconfirmed:
            _log(log_path, count, poc_path, "no result (cleanup of a killed attempt could not be confirmed)")
            notices.append(f"attempt {attempts} timed out and its process group could not be fully killed; "
                           "continuing, but this attempt's descendants may still be running")
            remaining_after = budget - (time.monotonic() - started)
            if remaining_after <= 1:
                break
            time.sleep(min(remaining_after - 1, 1.0))
            continue

        if reply and "exit_code" in reply:
            verdict = "no crash (exit %s)" % reply["exit_code"] if reply["exit_code"] in (0, 300) else "CRASH (exit %s)" % reply["exit_code"]
            _log(log_path, count, poc_path, verdict)
            last_reply = reply
            break
        if reply and RATE_LIMIT_PATTERN.search(str(reply.get("detail", ""))):
            detail = str(reply["detail"])  # the limit text comes from the server, never hard-coded here
            _log(log_path, count, poc_path, "RATE LIMITED (not graded)")
            last_reply = reply
            notices.append(f"rate limited: the server said {detail!r}; your submission was NOT graded; waiting and retrying.")
            remaining_after = budget - (time.monotonic() - started)
            if remaining_after <= 1:
                break
            limited_streak += 1
            wait = min(30.0, 5.0 * 2 ** (limited_streak - 1)) * random.uniform(0.8, 1.2)  # 5, 10, 20, 30, 30 ... s
            time.sleep(min(remaining_after - 1, wait))
            continue
        # anything else (killed by our own per-attempt bound, connection refused, empty/garbled reply): transient, retry.
        reason = "no answer" if completed.returncode in (-9, -1) or not raw.strip() else f"unrecognised reply: {raw[:200]!r}"
        _log(log_path, count, poc_path, f"no result ({reason})")
        notices.append(f"submission attempt {attempts} got {reason} after {attempt_seconds:.0f}s; retrying.")
        remaining_after = budget - (time.monotonic() - started)
        if remaining_after <= 1:
            break
        transient_streak += 1
        wait = min(16.0, 2.0 * 2 ** (transient_streak - 1)) * random.uniform(0.8, 1.2)  # 2, 4, 8, 16, 16 ... s
        time.sleep(min(remaining_after - 1, wait))

    history = _read_log_tail(log_path, poc_path_prefix_lines=attempts + 5)
    if last_reply and "exit_code" in last_reply:
        print(json.dumps(last_reply))
        if notices:
            print("\n".join(notices))
        print("\n[submission history]")
        print(history)
        return 0

    elapsed = time.monotonic() - started
    gave_up = attempts >= max_attempts and elapsed < budget - 1
    timeout_reply = {
        "timed_out": not gave_up, "gave_up": gave_up, "exit_code": None, "attempts": attempts,
        "error": (f"submission gave up after {attempts} attempts ({elapsed:.0f}s) without a verdict; it was NOT graded"
                  if gave_up else f"submission timed out after {elapsed:.0f}s"),
    }
    count = _log_count(log_path)
    _log(log_path, count, poc_path, "TIMED OUT (no verdict)")
    print(json.dumps(timeout_reply))
    if notices:
        print("\n".join(notices))
    print("\n[submission history]")
    print(_read_log_tail(log_path, poc_path_prefix_lines=attempts + 5))
    return 1


def _debug_ps(pgid: int) -> str:
    return subprocess.run(["ps", "-o", "pid,ppid,pgid,stat,cmd", "-g", str(pgid)], capture_output=True, text=True).stdout.replace("\n", " | ")


def _live_members(pgid: int) -> list[str]:
    """pids in this process group that are NOT zombies (state Z).

    A grandchild orphaned mid-kill gets reparented to PID 1, which reaps it asynchronously on its own
    schedule (not something we control or should wait on) -- until then it lingers as a zombie, still
    occupying the pgid, which makes a raw `kill(pgid, 0)` report "alive" even though nothing is actually
    running or holding any resource. Confirmed-gone has to mean "no real process left", not "no kernel
    bookkeeping entry left", or cleanup looks falsely slow/unconfirmed for something already harmless.
    Found live while testing: a kill-confirmation loop that waited for literal kernel-entry absence took
    up to several seconds purely waiting on PID 1's own reap cadence, with nothing real still running.
    """
    live = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        try:
            text = open(f"/proc/{name}/stat", encoding="utf-8", errors="replace").read()
            # comm (field 2) is "(name)" and may itself contain spaces/parens; read the rest after the
            # LAST ")" so later fields are found reliably regardless of what's inside comm.
            rest = text[text.rindex(")") + 2:].split()
            state, pgrp = rest[0], rest[2]  # field 3 (state), field 5 (pgrp), counting from field 3 as rest[0]
        except (OSError, IndexError, ValueError):
            continue
        if pgrp == str(pgid) and state != "Z":
            live.append(name)
    return live


def _run_bounded(argv: list[str], deadline: float) -> tuple[str, int]:
    """Run argv for at most `deadline` seconds, in its own process group, and kill the WHOLE group on timeout.

    A naive `timeout <N> cmd` only signals its direct child: if that child forks a grandchild (bash running
    curl, here) and the direct child dies first, the grandchild is orphaned and keeps running untouched --
    reproduced for real while testing this file (an orphaned curl survived for minutes after its parent was
    killed). setsid() makes the launched process its own session/process-group leader, so os.killpg reaches
    every descendant regardless of how many times it forked, and we confirm (poll os.killpg(pgid, 0)) that
    nothing answers before returning, escalating from SIGTERM to SIGKILL if needed.
    """
    import threading

    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    pgid = os.getpgid(process.pid)
    chunks: list[str] = []

    def read_output() -> None:
        try:
            for line in process.stdout:
                chunks.append(line)
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started = time.monotonic()
    while process.poll() is None and time.monotonic() - started < deadline:
        time.sleep(0.05)
    if process.poll() is not None:
        reader.join(timeout=2)
        return "".join(chunks), process.returncode

    debug = os.environ.get("CG_SUBMIT_DEBUG_KILL")

    def confirmed_gone() -> bool:
        process.poll()  # reap OUR OWN direct child promptly if it has already exited
        remaining = _live_members(pgid)
        if debug and remaining:
            print(f"DEBUG still alive, pgid={pgid} live={remaining} ps={_debug_ps(pgid)}", file=sys.stderr)
        return not remaining

    for sig, grace, poll in ((signal.SIGTERM, 1.0, 0.02), (signal.SIGKILL, 1.0, 0.02), (signal.SIGKILL, 2.0, 0.05)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            reader.join(timeout=2)  # the send itself found nothing left: that IS confirmation, not an open question
            return "".join(chunks), -9
        deadline_inner = time.monotonic() + grace
        while time.monotonic() < deadline_inner:
            if confirmed_gone():
                reader.join(timeout=2)
                return "".join(chunks), -9
            time.sleep(poll)
    reader.join(timeout=2)
    return "".join(chunks) + "\nCG_SUBMIT_CLEANUP_UNCONFIRMED", -9


def _inject_curl_timeouts(real_script: str, connect_timeout: float, max_attempt: float) -> str:
    """A patched copy of the generated script with curl-level --connect-timeout/--max-time added.

    Falls back to the unmodified script (relying on the outer `timeout` process as the only bound)
    if the expected curl invocation shape isn't found, rather than failing or silently doing nothing.
    """
    text = open(real_script, encoding="utf-8").read()
    patched, count = CURL_LINE.subn(
        f"curl -sS --connect-timeout {connect_timeout:g} --max-time {max_attempt:g} -X POST", text, count=1,
    )
    if count == 0:
        return real_script
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
    handle.write(patched)
    handle.close()
    os.chmod(handle.name, 0o700)
    return handle.name


def _parse_json_line(raw: str) -> dict | None:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _log_count(log_path: str) -> int:
    try:
        with open(log_path, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except FileNotFoundError:
        return 0


def _log(log_path: str, count: int, poc_path: str, verdict: str) -> None:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{count + 1}\t{poc_path}\t{verdict}\n")


def _read_log_tail(log_path: str, poc_path_prefix_lines: int) -> str:
    try:
        lines = open(log_path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return ""
    tail = lines[-poc_path_prefix_lines:] if poc_path_prefix_lines else lines
    out = []
    for line in tail:
        parts = line.split("\t")
        if len(parts) == 3:
            out.append(f"#{parts[0]} {parts[1]} -> {parts[2]}")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
