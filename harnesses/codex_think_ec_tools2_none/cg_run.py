#!/usr/bin/env python3
"""cg_run: run one command under a hard deadline and guarantee that every process it started is gone.

usage: cg_run --deadline-ms N --result-file PATH [--grace-ms M] -- COMMAND [ARG ...]

* --deadline-ms is mandatory (a whole number of milliseconds, 1..3600000). Without it cg_run refuses to run anything.
* The command runs in its own session. cg_run is a child subreaper (PR_SET_CHILD_SUBREAPER), so processes that are
  orphaned anywhere below the command (double fork, setsid, nohup, parent exits first) are re-parented to cg_run
  and stay visible and killable.
* One cleanup path serves every ending: normal exit, deadline, cancellation (SIGTERM/SIGINT/SIGHUP) and death of
  the parent (PR_SET_PDEATHSIG, plus a getppid() check). It freezes (SIGSTOP) and kills (SIGKILL) every descendant,
  reaps them, and then verifies against /proc that none remain. It never reports success while one remains.
* The command's stdout and stderr pass straight through. A structured result is written atomically to
  --result-file:
  {"timed_out","cancelled","cancel_reason","deadline_ms","exit_code","signal","duration_ms",
   "cleanup":{"descendants_found","killed","remaining","confirmed"}}
"""
import ctypes
import json
import os
import signal
import sys
import time

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
MAX_DEADLINE_MS = 3_600_000
DEFAULT_GRACE_MS = 1000
CLEANUP_HARD_CAP_S = 10.0
POLL_S = 0.02

_libc = ctypes.CDLL(None, use_errno=True)
_cancel = {"reason": None}


def _prctl(option, value=0):
    if _libc.prctl(option, ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def subreaper_enabled():
    flag = ctypes.c_int(0)
    if _libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) != 0:
        return False
    return flag.value == 1


def snapshot():
    """pid -> (state, ppid, pgrp, session) for every process in this PID namespace."""
    table = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        rest = raw[raw.rfind(")") + 2:].split()
        try:
            table[int(name)] = (rest[0], int(rest[1]), int(rest[2]), int(rest[3]))
        except (IndexError, ValueError):
            continue
    return table


def descendants(table, root, session=None):
    """Everything below root by parent links, plus anything still in the command's session."""
    children = {}
    for pid, (_, ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    if session is not None:
        found |= {pid for pid, (_, _, _, sid) in table.items() if sid == session}
    found.discard(root)
    return found


def reap(statuses):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        statuses[pid] = status


def _send(pid, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def live_descendants(statuses, root, session):
    """Reap what has died, then return the pids that are still alive (zombies do not count)."""
    reap(statuses)
    table = snapshot()
    return {pid for pid in descendants(table, root, session) if table.get(pid, ("Z",))[0] != "Z"}


def kill_everything(main_pid, statuses, grace_s, scan=live_descendants):
    """The single cleanup path. Returns (descendants_found, remaining_after_verification)."""
    me = os.getpid()
    seen = set()
    current = scan(statuses, me, main_pid)
    seen |= current
    if current:
        try:
            os.killpg(main_pid, signal.SIGTERM)
        except OSError:
            pass
        for pid in current:
            _send(pid, signal.SIGTERM)
        end = time.monotonic() + grace_s
        while current and time.monotonic() < end:
            time.sleep(POLL_S)
            current = scan(statuses, me, main_pid)
            seen |= current
    hard_stop = time.monotonic() + CLEANUP_HARD_CAP_S
    while current and time.monotonic() < hard_stop:
        for pid in current:
            _send(pid, signal.SIGSTOP)  # freeze first, so nothing can fork while it is being killed
        for pid in current:
            _send(pid, signal.SIGKILL)
        time.sleep(0.03)
        current = scan(statuses, me, main_pid)
        seen |= current
    remaining = scan(statuses, me, main_pid)
    if seen or remaining:
        time.sleep(0.05)  # verify twice when anything was involved: a late fork or a slow exit must not slip through
        remaining |= scan(statuses, me, main_pid)
    seen |= remaining
    seen.discard(main_pid)  # the command itself is not a "descendant found"; if it is still alive it stays in remaining
    return len(seen), len(remaining)


def write_result(path, result):
    if not path:
        return
    temporary = f"{path}.tmp{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        sys.stderr.write(f"cg_run: cannot write the result file: {error}\n")


def _on_signal(signum, _frame):
    _cancel["reason"] = signal.Signals(signum).name


def _refuse(message, result_file=None, code=2):
    sys.stderr.write(f"cg_run: {message}\n")
    write_result(result_file, {"refused": True, "error": message, "timed_out": False, "cancelled": False,
                               "cleanup": {"descendants_found": 0, "killed": 0, "remaining": 0, "confirmed": True}})
    return code


def parse(argv):
    options, command = {}, []
    if "--" in argv:
        split = argv.index("--")
        flags, command = argv[:split], argv[split + 1:]
    else:
        flags = argv
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in ("--deadline-ms", "--result-file", "--grace-ms") and index + 1 < len(flags):
            options[flag] = flags[index + 1]
            index += 2
        else:
            raise ValueError(f"unexpected argument {flag!r}")
    return options, command


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        options, command = parse(argv)
    except ValueError as error:
        return _refuse(str(error))
    result_file = options.get("--result-file")
    if "--deadline-ms" not in options:
        return _refuse("--deadline-ms is required: refusing to run a command with no deadline", result_file)
    try:
        deadline_ms = int(options["--deadline-ms"])
        grace_ms = int(options.get("--grace-ms", DEFAULT_GRACE_MS))
    except ValueError:
        return _refuse("--deadline-ms and --grace-ms must be whole numbers of milliseconds", result_file)
    if not 1 <= deadline_ms <= MAX_DEADLINE_MS or not 0 <= grace_ms <= 60_000:
        return _refuse(f"--deadline-ms must be between 1 and {MAX_DEADLINE_MS}", result_file)
    if not result_file:
        return _refuse("--result-file is required")
    if not command:
        return _refuse("no command given after --", result_file)

    started = time.monotonic()
    try:
        _prctl(PR_SET_CHILD_SUBREAPER, 1)
        if not subreaper_enabled():
            raise OSError("subreaper flag did not stick")
    except OSError as error:
        return _refuse(f"cannot become a child subreaper ({error}); cleanup could not be guaranteed", result_file, 97)
    original_parent = os.getppid()
    try:
        _prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM))
    except OSError:
        pass  # the getppid() check below still covers parent death
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    main_pid = os.fork()
    if main_pid == 0:
        try:
            os.setsid()
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
                signal.signal(sig, signal.SIG_DFL)
            os.execvp(command[0], command)
        except OSError as error:
            os.write(2, f"cg_run: cannot execute {command[0]}: {error.strerror}\n".encode())
            os._exit(127 if error.errno == 2 else 126)

    statuses = {}
    deadline = started + deadline_ms / 1000.0
    reason = None
    while True:
        reap(statuses)
        if _cancel["reason"] is not None:
            reason = "cancelled"
            break
        if os.getppid() != original_parent:
            _cancel["reason"] = "PARENT_EXIT"
            reason = "cancelled"
            break
        if main_pid in statuses:
            reason = "exited"
            break
        if time.monotonic() >= deadline:
            reason = "timed_out"
            break
        time.sleep(POLL_S)

    found, remaining = kill_everything(main_pid, statuses, grace_ms / 1000.0)
    reap(statuses)
    status = statuses.get(main_pid)
    exit_code = signal_name = None
    if status is not None:
        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            signal_name = signal.Signals(os.WTERMSIG(status)).name
    confirmed = remaining == 0
    result = {
        "timed_out": reason == "timed_out",
        "cancelled": reason == "cancelled",
        "cancel_reason": _cancel["reason"],
        "deadline_ms": deadline_ms,
        "exit_code": None if reason in ("timed_out", "cancelled") else exit_code,
        "signal": signal_name,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cleanup": {"descendants_found": found, "killed": max(found - remaining, 0), "remaining": remaining, "confirmed": confirmed},
    }
    write_result(result_file, result)
    if not confirmed:
        return 97
    if reason == "timed_out":
        return 124
    if reason == "cancelled":
        return 143
    if exit_code is not None:
        return exit_code
    if signal_name:
        return 128 + int(signal.Signals[signal_name])
    return 1


if __name__ == "__main__":
    sys.exit(main())
